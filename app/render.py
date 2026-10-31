"""Project -> video.

Pipeline: narration per scene (cached) -> frame-exact timeline -> ASS subtitles -> one motion
clip per scene (cached) -> a single FFmpeg pass that joins clips (cut / crossfade / fade through
black), burns subtitles and muxes the narration track.

Timing rule: every scene lasts lead-in + its measured narration + a pause, rounded UP to whole
frames, so audio and video can never drift apart. Transitions happen entirely inside the pause
at the end of a scene, so they never overlap or clip speech.
"""
import hashlib
import json
import math
import random
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import CONFIG, EXPORTS_DIR, LUTS_DIR, MY_DEFAULTS_FILE, WORKSPACE
from .tts import SAMPLE_RATE, Cancelled, ElevenLabs
from .captions import write_ass
from . import recording

SEGMENT_VERSION = 3
CANVAS_SCALE = 3                     # zoom/pan on a 3x canvas so movement is sub-pixel smooth
MOTIONS = ["static", "zoom_in", "zoom_out", "pan_left", "pan_right", "random"]
INTENSITY = {"subtle": 0.07, "medium": 0.12, "strong": 0.18}
QUALITY = {"standard": ("medium", 21), "high": ("medium", 18), "max": ("slow", 16)}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SEGMENT_DIR = WORKSPACE / "cache" / "segments"
AUDIO_DIR = WORKSPACE / "cache" / "audio"
PREVIEW_DIR = WORKSPACE / "preview"
JOBS_DIR = WORKSPACE / "jobs"
for _d in (SEGMENT_DIR, AUDIO_DIR, PREVIEW_DIR, JOBS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# FFmpeg xfade transitions offered in the app (id -> label). "cut" = no transition.
TRANSITIONS = {
    "cut": "Simple cut", "fade": "Crossfade", "dissolve": "Dissolve", "fadeslow": "Slow fade",
    "fadeblack": "Fade through black", "fadewhite": "Fade through white", "fadegrays": "Fade through gray",
    "hblur": "Blur", "zoomin": "Zoom in", "pixelize": "Pixelize",
    "slideleft": "Slide left", "slideright": "Slide right", "slideup": "Slide up", "slidedown": "Slide down",
    "smoothleft": "Smooth left", "smoothright": "Smooth right",
    "wipeleft": "Wipe left", "wiperight": "Wipe right", "circleopen": "Circle open",
    "circleclose": "Circle close", "radial": "Radial",
}

# "Fade through black/white" like video editors do it: the picture fades fully out, the color holds
# for a moment, then the next picture fades in. (FFmpeg's own fadeblack/fadewhite only touch the
# color for an instant, which looks like a quick dark crossfade.)
DIP_LUMA = {"fadeblack": 16, "fadewhite": 235}     # Y of black / white in TV range; chroma stays neutral
DIP_HOLD = 0.2                                     # share of the transition that is fully black / white


def dip_expr(kind):
    """xfade custom expression. P runs from 1 to 0 over the transition, so 1-P is the progress."""
    edge = (1 - DIP_HOLD) / 2
    color = f"if(PLANE,128,{DIP_LUMA[kind]})"
    out = f"max(0,1-(1-P)/{edge:.4f})"                       # first picture: 1 -> 0
    into = f"max(0,((1-P)-{edge + DIP_HOLD:.4f})/{edge:.4f})"  # next picture: 0 -> 1
    return (f"if(lte(1-P,0.5),{color}+(A-{color})*{out},{color}+(B-{color})*{into})")


def builtin_looks():
    """Names of the built-in color grades (the .cube files in luts/)."""
    return sorted(p.stem for p in LUTS_DIR.glob("*.cube"))


DEFAULT_LOOK = "Anime 03" if "Anime 03" in builtin_looks() else ""

DEFAULT_SETTINGS = {
    "voice": {"voice": "am_liam", "speed": 1.0, "style": "story"},
    "timing": {"lead_in": 0.15, "scene_pause": 0.3, "sentence_pause": 0.2, "end_hold": 1.0,
               "silent_scene": 3.0},       # seconds a scene with an image but no narration is shown
    "subtitles": {"enabled": True, "font": "Arial Black", "size": 84, "color": "#FFFFFF",
                  "outline_color": "#000000", "outline": 6, "shadow": 2, "bold": True, "italic": False,
                  "uppercase": False, "position": 26, "position_x": 50, "max_width": 84,
                  "words_per_caption": 5},
    "imagegen": {"style": "", "no_text": True, "references": [], "continuity": True,
                 "places": "new"},        # "new": every scene is a different place; "story": stay until the story moves
    "story": {"continues_from": "",        # project file of the episode this one continues
              "world": ""},                # what the world looks like now (e.g. "a week into a zombie outbreak")
    "watermark": {"enabled": False, "text": "", "font": "Arial", "size": 40, "color": "#FFFFFF",
                  "opacity": 70, "outline": 2, "outline_color": "#000000", "shadow": 1, "bold": True, "italic": False,
                  "anchor": "br", "position_x": 96, "position_y": 96},
    "music": {"enabled": False, "file": "", "name": "", "volume": 15, "duck": True, "fade_out": 2.0},
    # Color grade over the whole video: a built-in Look (luts/<look>.cube), or your own .cube file (lut wins).
    "color": {"look": DEFAULT_LOOK, "lut": "", "lut_name": "", "strength": 100},
    "motion": {"default": "random", "intensity": "subtle", "fit": "contain"},
    "transition": {"type": "cut", "duration": 1.0,
                   "fade_in": 0.0, "fade_out": 0.0},    # seconds of fade from / to black at the very start / end
    "export": {"output_dir": str(EXPORTS_DIR), "filename": "", "resolution": "1080x1920",
               "fps": 30, "quality": "high"},
}
BUILTIN_SETTINGS = json.loads(json.dumps(DEFAULT_SETTINGS))
# Settings that belong to one episode only, never to "my defaults".
NOT_DEFAULTS = {"story": None, "export": {"filename", "output_dir"}, "imagegen": {"references"}}


def defaults_from(settings):
    """The part of a project's settings that can become the starting point for new projects."""
    out = {}
    for section, builtin in BUILTIN_SETTINGS.items():
        skip = NOT_DEFAULTS.get(section, set())
        if skip is None:
            continue
        given = settings.get(section) or {}
        out[section] = {k: given[k] for k in builtin if k in given and k not in skip}
    return out


def apply_my_defaults(mine=None):
    """DEFAULT_SETTINGS = built-in values, overridden by my_defaults.json (or `mine`)."""
    if mine is None:
        try:
            mine = json.loads(MY_DEFAULTS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mine = {}
    for section, builtin in BUILTIN_SETTINGS.items():
        DEFAULT_SETTINGS[section] = {**json.loads(json.dumps(builtin)), **(mine.get(section) or {})}


apply_my_defaults()


class RenderError(Exception):
    pass


def merged_settings(project):
    """Project settings with defaults filled in for anything missing."""
    given = project.get("settings") or {}
    out = {}
    for section, defaults in DEFAULT_SETTINGS.items():
        out[section] = {**defaults, **(given.get(section) or {})}
    return out


# ---- jobs ------------------------------------------------------------------------------------
class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.state = "running"          # running | done | error | cancelled
        self.stage = "Starting…"
        self.progress = 0.0
        self.error = None
        self.result = None
        self.started = time.time()
        self.proc = None
        self._cancel = threading.Event()

    def update(self, stage=None, progress=None):
        if stage is not None:
            self.stage = stage
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))

    def cancel(self):
        self._cancel.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()

    def cancelled(self):
        return self._cancel.is_set()

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "state": self.state, "stage": self.stage,
                "progress": round(self.progress, 4), "error": self.error, "result": self.result,
                "elapsed": round(time.time() - self.started, 1)}


def run_ffmpeg(args, job, cwd=None, duration=None, on_progress=None):
    cmd = [CONFIG["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    job.proc = proc
    errors = []
    reader = threading.Thread(target=lambda: errors.extend(proc.stderr.readlines()), daemon=True)
    reader.start()
    for line in proc.stdout:
        if job.cancelled():
            proc.kill()
            break
        if duration and on_progress and line.startswith("out_time_us="):
            try:
                on_progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
            except ValueError:
                pass
    proc.wait()
    reader.join(timeout=5)
    job.proc = None
    if job.cancelled():
        raise Cancelled()
    if proc.returncode != 0:
        detail = "".join(errors[-12:]).strip() or f"exit code {proc.returncode}"
        raise RenderError(f"FFmpeg failed: {detail}")


_probe_cache = {}


def image_size(path):
    st = Path(path).stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _probe_cache:
        out = subprocess.run([CONFIG["ffprobe"], "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "json", str(path)],
                             capture_output=True, text=True, creationflags=NO_WINDOW)
        try:
            stream = json.loads(out.stdout)["streams"][0]
            _probe_cache[key] = (int(stream["width"]), int(stream["height"]))
        except (ValueError, KeyError, IndexError):
            raise RenderError(f"Could not read image: {Path(path).name}. Is it a valid PNG/JPG/WebP?")
    return _probe_cache[key]


# ---- narration + timeline --------------------------------------------------------------------
def validate(project):
    problems = []
    scenes = project.get("scenes") or []
    if not scenes:
        problems.append("Add at least one scene.")
    for i, scene in enumerate(scenes, 1):
        image = scene.get("image")
        if image and not Path(image).exists():      # no image at all is fine: that scene is black
            problems.append(f"Scene {i}: image not found ({Path(image).name}). Use Replace to locate it.")
        if not (scene.get("script") or "").strip() and not image and not recording.of(project):
            problems.append(f"Scene {i} is empty: add narration or an image.")   # an image alone is a silent scene
    lut = lut_path(merged_settings(project)["color"])
    if lut and not Path(lut).is_file():
        problems.append(f"Color grade file not found ({Path(lut).name}). Choose it again (Settings → Color grade).")
    rec = recording.of(project)
    if rec and not Path(rec["file"]).is_file():
        problems.append(f"Your recording was not found ({Path(rec['file']).name}). Choose it again in Settings → Voice.")
    elif rec and not rec.get("words"):
        problems.append("Your recording hasn't been listened to yet: click 🎧 Make scenes from recording (Settings → Voice).")
    music = merged_settings(project)["music"]
    if music["enabled"] and not rec:      # a recording brings its own music
        if not music["file"]:
            problems.append("Background music is on but no music file is chosen (Settings → Background music).")
        elif not Path(music["file"]).is_file():
            problems.append(f"Background music file not found ({Path(music['file']).name}). Choose it again.")
    if problems:
        raise RenderError("\n".join(problems))


def scene_audio_key(script, settings):
    v, t = settings["voice"], settings["timing"]
    parts = [v["voice"], round(float(v["speed"]), 3), float(t["sentence_pause"]), script]
    if v.get("style", "story") != "story":       # left out for storytelling so existing caches stay valid
        parts.append(v["style"])
    if str(v["voice"]).startswith("eleven:"):    # a new model gives new audio, so a new file name
        parts.append(ElevenLabs.model)
    raw = json.dumps(parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def narrate(project, narrator, job, progress_span=(0.0, 1.0)):
    """Generate (or load cached) narration for every scene. Returns per-scene dicts."""
    if recording.of(project):
        return narrate_recording(project, job, progress_span)
    s = merged_settings(project)
    voice, speed = s["voice"]["voice"], float(s["voice"]["speed"])
    scenes = project["scenes"]
    out = []
    a, b = progress_span
    for i, scene in enumerate(scenes):
        job.update(f"Narration: scene {i + 1} of {len(scenes)}", a + (b - a) * i / len(scenes))
        script = (scene.get("script") or "").strip()
        if script:
            audio, captions, fresh = narrator.scene(
                scene["script"], voice, speed, float(s["timing"]["sentence_pause"]),
                int(s["subtitles"]["words_per_caption"]), cancelled=job.cancelled,
                style=s["voice"].get("style", "story"))
            if not len(audio):
                raise RenderError(f"Scene {i + 1}: narration produced no audio. Check the script text.")
            key = scene_audio_key(scene["script"], s)
        else:   # image only: silence for the chosen length, no captions
            hold = max(0.5, float(s["timing"].get("silent_scene", 3.0)))
            audio, captions = np.zeros(int(round(hold * SAMPLE_RATE)), dtype=np.float32), []
            key = scene_audio_key(f"<silent {hold}>", s)
        wav = AUDIO_DIR / f"{key}.wav"
        if not wav.exists():
            sf.write(str(wav), audio, SAMPLE_RATE)
        out.append({"id": scene.get("id"), "audio": audio, "captions": captions,
                    "speech": len(audio) / SAMPLE_RATE, "audio_url": f"/api/audio/{key}.wav"})
    return out


def narrate_recording(project, job, progress_span=(0.0, 1.0)):
    """Your own recording: each scene is its stretch of the recording, captions timed by Whisper."""
    rec = recording.of(project)
    s = merged_settings(project)
    job.update("Reading your recording", progress_span[0])
    audio = recording.decoded(rec["file"])
    duration = len(audio) / SAMPLE_RATE
    words, out = recording.refine_words(rec.get("words") or [], audio), []     # also fixes projects made before
    for scene, (start, end) in zip(project["scenes"], recording.bounds(project["scenes"], duration)):
        piece = audio[int(round(start * SAMPLE_RATE)):int(round(end * SAMPLE_RATE))]
        key = hashlib.sha1(json.dumps([rec["file"], round(start, 3), round(end, 3)]).encode()).hexdigest()[:20]
        wav = AUDIO_DIR / f"rec_{key}.wav"
        if not wav.exists():
            sf.write(str(wav), piece, SAMPLE_RATE)
        out.append({"id": scene.get("id"), "audio": piece, "speech": end - start, "rec_start": start, "rec_end": end,
                    "captions": recording.scene_captions(scene.get("script"), words, start, end,
                                                         int(s["subtitles"]["words_per_caption"])),
                    "audio_url": f"/api/audio/rec_{key}.wav"})
    return out


def build_timeline(project, narration):
    """Frame-exact scene durations, the full narration track and global caption events."""
    s = merged_settings(project)
    fps = int(s["export"]["fps"])
    t = s["timing"]
    trans = s["transition"]
    kinds = resolve_transitions(project)
    length = max(1, round(float(trans["duration"]) * fps))
    d_in = [0 if k == "cut" else length for k in kinds]      # overlap frames going INTO each scene
    fade_out = max(0.0, float(trans.get("fade_out") or 0))
    lead_in = max(0.0, float(t["lead_in"]))
    n = len(narration)
    scenes, frame = [], 0
    rec = recording.of(project)
    if rec:      # the recording sets the clock: scenes start exactly where they start in it
        lead_in = 0.0
        # A transition is centered on the scene change, so a fade through black is darkest in the
        # middle of the pause: the next picture's own frames start half a transition later.
        edges = [0] + [round(item["rec_start"] * fps + d_in[i] / 2) for i, item in enumerate(narration) if i] \
            + [math.ceil(narration[-1]["rec_end"] * fps - 1e-6)]
        for i, item in enumerate(narration):
            frames = max(1, edges[i + 1] - edges[i])
            scenes.append({**item, "start_frame": frame, "frames": frames, "start": frame / fps, "duration": frames / fps})
            frame += frames
    for i, item in ([] if rec else enumerate(narration)):
        last = i == n - 1
        if last:
            tail = max(0.2, float(t["end_hold"]), fade_out + 0.1 if fade_out else 0)   # never fade the speech
        else:
            tail = max(0.0, float(t["scene_pause"]))
            if d_in[i + 1]:
                tail = max(tail, d_in[i + 1] / fps + 0.15)   # the whole transition fits in the pause
        frames = math.ceil((lead_in + item["speech"] + tail) * fps - 1e-6)
        scenes.append({**item, "start_frame": frame, "frames": frames,
                       "start": frame / fps, "duration": frames / fps})
        frame += frames
    total_frames = frame
    total = total_frames / fps

    track = None if rec else np.zeros(int(round(total * SAMPLE_RATE)) + 1, dtype=np.float32)
    events = []
    for i, sc in enumerate(scenes):
        if track is not None:
            at = int(round((sc["start"] + lead_in) * SAMPLE_RATE))
            track[at:at + len(sc["audio"])] = sc["audio"][:len(track) - at]
        visual_end = (scenes[i + 1]["start"] - d_in[i + 1] / fps) if i < n - 1 else total
        base = sc["rec_start"] if rec else sc["start"] + lead_in     # captions follow the recording's clock
        for c in sc["captions"]:
            events.append({"scene": i, "start": base + c["start"], "end": base + c["end"], "text": c["text"],
                           "limit": visual_end})
    # A caption stays on screen until the next one appears (also through a pause in the voice); the
    # last caption of a scene stays until the scene's transition starts (the video's end for the last).
    for j, ev in enumerate(events):
        nxt = events[j + 1] if j + 1 < len(events) else None
        if nxt and nxt["scene"] == ev["scene"]:
            ev["end"] = nxt["start"]
        else:
            ev["end"] = min(ev["limit"], nxt["start"] if nxt else total)
        ev["end"] = max(ev["end"], ev["start"] + 0.2)
    return {"fps": fps, "scenes": scenes, "total": total, "total_frames": total_frames,
            "track": track, "events": events, "d_in": d_in, "kinds": kinds,
            "audio_file": rec["file"] if rec else None}      # a recording is used as it is, at full quality


def resolve_transitions(project):
    """Transition going INTO each scene ("cut" for the first). A scene's own choice is the transition
    to the next scene; "default" uses the project setting."""
    s = merged_settings(project)
    kinds = ["cut"]
    for scene in project["scenes"][:-1]:
        kind = scene.get("transition") or "default"
        if kind == "default":
            kind = s["transition"]["type"]
        kinds.append(kind if kind in TRANSITIONS else "fade")
    return kinds[:len(project["scenes"])]


# ---- per-scene motion clips ------------------------------------------------------------------
RANDOM_MOVES = ("zoom_in", "zoom_out", "pan_left", "pan_right")


def resolve_motions(project):
    """Movement per scene. "Random" works like a shuffled deck: all four moves are used before one
    repeats, and a zoom is followed by a pan (and the other way round) whenever possible, so the
    video never goes zoom in, zoom out, zoom in… Scenes with their own movement count as dealt."""
    s = merged_settings(project)
    scenes = project["scenes"]
    # The last scene ends the video with a slow zoom out unless it has its own movement; a random scene
    # before it never uses the last scene's movement, so the ending doesn't repeat.
    ending = bool(scenes) and (scenes[-1].get("motion") or "default") == "default"
    last_move = "zoom_out" if ending else (scenes[-1].get("motion") if scenes else None)
    result, previous, dealt = [], None, set()
    for i, scene in enumerate(scenes):
        motion = scene.get("motion") or "default"
        if motion == "default":
            motion = "zoom_out" if ending and i == len(scenes) - 1 else s["motion"]["default"]
        random_pick = motion == "random"
        if random_pick:
            rng = random.Random(f"{scene.get('id')}-{i}")
            if dealt >= set(RANDOM_MOVES):
                dealt = set()                 # a new round of the deck
            banned = {previous} | ({last_move} if i == len(scenes) - 2 else set())
            options = [m for m in RANDOM_MOVES if m not in dealt and m not in banned] or \
                [m for m in RANDOM_MOVES if m not in banned]
            other_kind = [m for m in options if previous is None or m.split("_")[0] != previous.split("_")[0]]
            motion = rng.choice(other_kind or options)
        result.append((motion, random_pick))
        if motion in RANDOM_MOVES:
            dealt.add(motion)
        previous = motion
    return result


def _fit_mode(fit):
    # Older projects saved "auto"/"blur"; those now use contain so artwork is never cropped.
    return "cover" if fit == "cover" else "contain"


def _contain_box(size, cw, ch, scale=1.0):
    """Position (x, y, w, h) of the whole image fitted inside a cw x ch canvas, centered."""
    iw, ih = size
    f = min(cw * scale / iw, ch * scale / ih)
    bw, bh = max(2, int(iw * f) // 2 * 2), max(2, int(ih * f) // 2 * 2)
    return (cw - bw) // 2, (ch - bh) // 2, bw, bh


def segment_filter(motion, frames, width, height, fps, fit, intensity, size=None, allow_zoom_fallback=False):
    """Returns (filter_graph, effective_motion)."""
    cw, ch = width * CANVAS_SCALE, height * CANVAS_SCALE
    r = INTENSITY.get(intensity, INTENSITY["subtle"])
    p = f"(on/{max(frames - 1, 1)})"
    cx, cy = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    if fit == "contain":
        # The picture sits at a FIXED place on the black background (edges never move); the camera
        # moves inside the picture. During a move a thin band at its edges is briefly out of view
        # (zoom: up to r, pan: r + 1%); zoom-in starts and zoom-out ends on the whole picture.
        bx, by, bw, bh = _contain_box(size, width, height)
        sw, sh = bw * CANVAS_SCALE, bh * CANVAS_SCALE          # draw the moves on a 3x copy: sub-pixel smooth
        if motion == "zoom_in":
            z, x, y = f"1+{r}*{p}", cx, cy
        elif motion == "zoom_out":
            z, x, y = f"{1 + r}-{r}*{p}", cx, cy
        elif motion == "pan_left":
            z, x, y = f"{1 + r + 0.01}", f"(iw-iw/zoom)*(1-{p})", cy
        elif motion == "pan_right":
            z, x, y = f"{1 + r + 0.01}", f"(iw-iw/zoom)*{p}", cy
        else:
            motion, z, x, y = "static", "1", "0", "0"
        return (f"[0:v]scale={sw}:{sh}:flags=lanczos,setsar=1,format=rgb24[c];"
                f"[c]zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={bw}x{bh}:fps={fps},"
                f"pad={width}:{height}:{bx}:{by}:color=black,scale=out_range=tv,format=yuv420p[v]"), motion
    canvas = (f"[0:v]scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={cw}:{ch},setsar=1[c]")
    if motion == "zoom_in":
        z, x, y = f"1+{r}*{p}", cx, cy
    elif motion == "zoom_out":
        z, x, y = f"{1 + r}-{r}*{p}", cx, cy
    elif motion == "pan_left":
        z, x, y = f"{1 + r + 0.03}", f"(iw-iw/zoom)*(1-{p})", cy
    elif motion == "pan_right":
        z, x, y = f"{1 + r + 0.03}", f"(iw-iw/zoom)*{p}", cy
    else:
        z, x, y = "1", "0", "0"
    return (f"{canvas};[c]zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},"
            f"scale=out_range=tv,format=yuv420p[v]"), motion


def render_segment(image, motion, frames, width, height, fps, fit, intensity, preview, job,
                   allow_zoom_fallback=False):
    """Render (or reuse) one scene clip. Returns (path, movement actually used)."""
    if not image:
        return render_blank(frames, width, height, fps, preview, job), "static"
    st = Path(image).stat()
    fit = _fit_mode(fit)
    graph, used = segment_filter(motion, frames, width, height, fps, fit, intensity,
                                 image_size(image), allow_zoom_fallback)
    key = hashlib.sha1(json.dumps([SEGMENT_VERSION, str(image), st.st_mtime, st.st_size, graph,
                                   frames, preview]).encode())
    out = SEGMENT_DIR / f"{key.hexdigest()[:24]}.mp4"
    if out.exists():
        out.touch()
        return out, used
    # Intermediate clips: re-encoded once more in the final pass, so CRF 15 is visually lossless.
    preset, crf = ("veryfast", 23) if preview else ("veryfast", 15)
    tmp = out.with_name(out.stem + ".part.mp4")
    try:
        run_ffmpeg(["-i", str(image), "-filter_complex", graph,
                    "-map", "[v]", "-frames:v", str(frames), "-an", "-c:v", "libx264",
                    "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", str(tmp)], job)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out, used


def music_filters(nar, mus, music, total):
    """Mix looping background music under the narration: set volume, lower it while the narrator
    speaks (optional), fade it out at the end. Output label [aout], exactly as long as the video."""
    fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    vol = max(0.0, min(100.0, float(music["volume"]))) / 100
    fade = max(0.0, min(float(music["fade_out"]), total))
    parts = [f"[{nar}:a]{fmt},asplit=2[nar][key]" if music["duck"] else f"[{nar}:a]{fmt}[nar]",
             f"[{mus}:a]{fmt},volume={vol:.3f}[m0]"]
    last = "m0"
    if music["duck"]:
        parts.append(f"[m0][key]sidechaincompress=threshold=0.03:ratio=4:attack=30:release=600[m1]")
        last = "m1"
    if fade > 0:
        parts.append(f"[{last}]afade=t=out:st={total - fade:.3f}:d={fade:.3f}[m2]")
        last = "m2"
    parts.append(f"[nar][{last}]amix=inputs=2:duration=first:normalize=0[aout]")
    return parts


def render_blank(frames, width, height, fps, preview, job):
    """A black clip for a scene that has no image yet, so the video can be exported anyway."""
    key = hashlib.sha1(json.dumps([SEGMENT_VERSION, "blank", frames, width, height, fps, preview]).encode())
    out = SEGMENT_DIR / f"{key.hexdigest()[:24]}.mp4"
    if out.exists():
        out.touch()
        return out
    tmp = out.with_name(out.stem + ".part.mp4")
    try:
        run_ffmpeg(["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
                    "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-color_range", "tv", str(tmp)], job)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


# ---- full render -----------------------------------------------------------------------------
def render(project, narrator, job, preview):
    """Render the project. Preview = half resolution, fast encode, same timeline."""
    if not CONFIG.get("ffmpeg"):
        raise RenderError("FFmpeg was not found. Install it or set 'ffmpeg' in config.json.")
    validate(project)
    s = merged_settings(project)
    narration = narrate(project, narrator, job, (0.0, 0.25))
    tl = build_timeline(project, narration)
    fps = tl["fps"]
    width, height = parse_resolution(s["export"]["resolution"])
    if preview:
        width, height = width // 2, height // 2
        width, height = width - width % 2, height - height % 2
    motions = resolve_motions(project)
    work = JOBS_DIR / job.id
    work.mkdir(parents=True, exist_ok=True)
    final_tmp = None
    try:
        if tl["audio_file"] is None:
            sf.write(str(work / "narration.wav"), tl["track"], SAMPLE_RATE)
        subs, wm = s["subtitles"], s["watermark"]
        burn = subs["enabled"] or (wm["enabled"] and wm["text"].strip())
        if burn:
            write_ass(work / "subs.ass", tl["events"] if subs["enabled"] else [], subs, width, height,
                      watermark=wm, duration=tl["total"])

        d_in = tl["d_in"]
        clips, used, n = [], [], len(tl["scenes"])
        for i, sc in enumerate(tl["scenes"]):
            if job.cancelled():
                raise Cancelled()
            motion, random_pick = motions[i]
            job.update(f"Rendering scene {i + 1} of {n} ({motion.replace('_', ' ')})",
                       0.25 + 0.35 * i / n)
            frames = sc["frames"] + d_in[i]
            clip, actual = render_segment(project["scenes"][i]["image"], motion, frames, width,
                                          height, fps, s["motion"]["fit"], s["motion"]["intensity"],
                                          preview, job, allow_zoom_fallback=random_pick)
            clips.append(clip)
            used.append(actual)

        args = []
        for clip in clips:
            args += ["-i", str(clip)]
        args += ["-i", tl["audio_file"] or "narration.wav"]
        music = s["music"]
        use_music = music["enabled"] and music["file"] and float(music["volume"]) > 0 and not tl["audio_file"]
        if use_music:
            args += ["-stream_loop", "-1", "-i", str(music["file"])]      # loops for the whole video
        # The color grade is applied to every scene clip BEFORE the transitions, ending in YUV again: the
        # transitions (fade through black/white set Y and U/V values) then stay in YUV, so black stays
        # black (when RGB follows them, FFmpeg runs them in RGB and black turns purple).
        grade = color_grade_filter(s["color"], work)
        parts = []
        for i in range(n):
            if grade:
                parts.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,format=gbrp,split[go{i}][gs{i}];"
                             f"[gs{i}]{grade}[gg{i}];[go{i}][gg{i}]blend=all_expr='A*{1 - grade_k(s['color']):.3f}+"
                             f"B*{grade_k(s['color']):.3f}',format=yuv420p[v{i}]"
                             if grade_k(s["color"]) < 0.999 else
                             f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,format=gbrp,{grade},format=yuv420p[v{i}]")
            else:
                parts.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
        if n == 1:
            last = "v0"
        elif not any(d_in):
            parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
            last = "vc"
        else:
            last = "v0"
            for i in range(1, n):
                if d_in[i]:
                    offset = (tl["scenes"][i]["start_frame"] - d_in[i]) / fps
                    kind = tl["kinds"][i]
                    how = f"transition=custom:expr='{dip_expr(kind)}'" if kind in DIP_LUMA else f"transition={kind}"
                    parts.append(f"[{last}][v{i}]xfade={how}:duration={d_in[i] / fps:.6f}:"
                                 f"offset={offset:.6f}[x{i}]")
                else:
                    parts.append(f"[{last}][v{i}]concat=n=2:v=1:a=0[x{i}]")
                last = f"x{i}"
        sub_filter = "ass=subs.ass," if burn else ""
        fade_in = max(0.0, min(float(s["transition"].get("fade_in") or 0), tl["total"]))
        fade_out = max(0.0, min(float(s["transition"].get("fade_out") or 0), tl["total"]))
        fades = (f"fade=t=in:st=0:d={fade_in:.3f}," if fade_in else "") + \
                (f"fade=t=out:st={tl['total'] - fade_out:.3f}:d={fade_out:.3f}," if fade_out else "")
        parts.append(f"[{last}]{sub_filter}{fades}scale=out_range=tv,format=yuv420p[out]")  # JPGs are full-range; output standard TV range
        audio_map = f"{n}:a"
        if use_music:
            parts += music_filters(n, n + 1, music, tl["total"])
            audio_map = "[aout]"
        if fade_out:      # the sound (music) fades out with the picture
            parts.append(f"[{audio_map.strip('[]')}]afade=t=out:st={tl['total'] - fade_out:.3f}:d={fade_out:.3f}[afin]")
            audio_map = "[afin]"
        if preview:
            preset, crf = "ultrafast", 26
        else:
            preset, crf = QUALITY.get(s["export"]["quality"], QUALITY["high"])
        enc = ["-filter_complex", ";".join(parts), "-map", "[out]", "-map", audio_map,
               "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high",
               "-pix_fmt", "yuv420p", "-color_range", "tv", "-r", str(fps), "-c:a", "aac", "-b:a", "192k",
               "-ar", "48000", "-ac", "2", "-t", f"{tl['total']:.6f}", "-movflags", "+faststart"]

        if preview:
            for old in PREVIEW_DIR.glob("preview_*.mp4"):
                old.unlink(missing_ok=True)
            final = PREVIEW_DIR / f"preview_{job.id}.mp4"
        else:
            out_dir = Path(s["export"]["output_dir"] or EXPORTS_DIR)
            out_dir.mkdir(parents=True, exist_ok=True)
            base = _safe_name(s["export"]["filename"] or project.get("name") or "StoryCraft")
            final = out_dir / f"{base}.mp4"
            k = 2
            while final.exists():
                final = out_dir / f"{base} ({k}).mp4"
                k += 1
        final_tmp = final.with_name(final.stem + ".partial.mp4")
        job.update("Assembling video, transitions and subtitles", 0.6)
        run_ffmpeg(args + enc + [str(final_tmp)], job, cwd=str(work), duration=tl["total"],
                   on_progress=lambda f: job.update(f"Assembling video: {int(f * 100)}%",
                                                    0.6 + 0.4 * f))
        final_tmp.replace(final)
        final_tmp = None
        return {"path": str(final), "duration": round(tl["total"], 3), "width": width,
                "height": height, "scenes": scene_summary(tl), "motions": used}
    finally:
        if final_tmp is not None:
            Path(final_tmp).unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


RESOLUTIONS = {"1080x1920", "720x1280"}     # always vertical 9:16 (Shorts / Reels)


def lut_kind(path):
    """'lut3d' or 'lut1d' for a .cube file, None if it isn't one."""
    try:
        head = Path(path).read_text(encoding="utf-8", errors="replace")[:20000].upper()
    except OSError:
        return None
    return "lut3d" if "LUT_3D_SIZE" in head else "lut1d" if "LUT_1D_SIZE" in head else None


def lut_path(color):
    """The .cube file to use: your own file, else the built-in Look, else '' (original colors)."""
    if color.get("lut"):
        return color["lut"]
    return str(LUTS_DIR / f"{color['look']}.cube") if color.get("look") else ""


def grade_k(color):
    """Color grade strength, 0..1 (below 1 the graded picture is mixed with the original)."""
    return max(0.0, min(float(color.get("strength", 100)), 100.0)) / 100


def color_grade_filter(color, work):
    """The LUT filter for the chosen color grade ('' for none). Captions are added later, so they keep
    their own colors."""
    lut = lut_path(color)
    kind = lut_kind(lut) if lut else None
    if not kind or grade_k(color) <= 0:
        return ""
    shutil.copyfile(lut, work / "grade.cube")      # a plain relative name: no Windows path escaping in the filter
    return f"{kind}=file=grade.cube"


def parse_resolution(value):
    """'1080x1920' -> (1080, 1920). A 16:9 value from an older project is turned upright;
    anything unknown falls back to 1080x1920."""
    w, _, h = str(value).partition("x")
    value = f"{h}x{w}" if f"{h}x{w}" in RESOLUTIONS else value
    value = value if value in RESOLUTIONS else "1080x1920"
    w, h = value.split("x")
    return int(w), int(h)


def scene_summary(tl):
    return [{"id": sc["id"], "duration": round(sc["duration"], 3), "speech": round(sc["speech"], 3),
             "start": round(sc["start"], 3), "audio_url": sc["audio_url"],
             "captions": [{"start": round(c["start"], 3), "end": round(c["end"], 3), "text": c["text"]}
                          for c in sc["captions"]]} for sc in tl["scenes"]]


def _safe_name(name):
    cleaned = "".join(ch for ch in name if ch not in '<>:"/\\|?*').strip().rstrip(".")
    return cleaned or "StoryCraft"


SEGMENT_CACHE_MB = 300
SEGMENT_MAX_AGE_DAYS = 7


def trim_segments(max_mb=SEGMENT_CACHE_MB, max_age_days=SEGMENT_MAX_AGE_DAYS):
    """Keep the clip cache small: drop clips older than a week, then the least recently used ones
    until the cache is under max_mb. Clips used in the last 10 minutes (the current render) stay."""
    now = time.time()
    files = []
    for f in SEGMENT_DIR.glob("*.mp4"):
        try:
            st = f.stat()
        except OSError:
            continue
        if f.name.endswith(".part.mp4") or st.st_mtime < now - max_age_days * 86400:
            f.unlink(missing_ok=True)
        else:
            files.append((st.st_mtime, st.st_size, f))
    total = sum(size for _, size, _ in files)
    for mtime, size, f in sorted(files):            # oldest use first
        if total <= max_mb * 1024 * 1024 or mtime > now - 600:
            break
        f.unlink(missing_ok=True)
        total -= size


def _dir_size(d, pattern="*"):
    return sum(f.stat().st_size for f in d.glob(pattern) if f.is_file())


def cache_info():
    return {"clips_mb": round(_dir_size(SEGMENT_DIR) / 1048576, 1),
            "previews_mb": round(_dir_size(PREVIEW_DIR) / 1048576, 1),
            "narration_mb": round((_dir_size(AUDIO_DIR) + _dir_size(WORKSPACE / "cache" / "tts")) / 1048576, 1)}


def clear_video_cache():
    """Delete cached clips and old previews (rebuilt automatically). Narration cache is kept."""
    for f in list(SEGMENT_DIR.glob("*.mp4")) + list(PREVIEW_DIR.glob("*.mp4")):
        f.unlink(missing_ok=True)


def cleanup_cache():
    """At startup: trim the clip cache and remove temp folders left by an interrupted render."""
    trim_segments()
    for d in JOBS_DIR.iterdir():
        shutil.rmtree(d, ignore_errors=True)
