"""StoryCraft video maker: local web server. Run with `python -m app.main` (or start.bat)."""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import traceback
import webbrowser
from pathlib import Path

import soundfile as sf
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import dialogs, imagegen, recording
from . import config as config_module
from .config import ARTSTYLE_DIR, CONFIG, MEDIA_DIR, MY_DEFAULTS_FILE, PROJECTS_DIR, ROOT, WORKSPACE
from .render import (AUDIO_DIR, DEFAULT_SETTINGS, MOTIONS, PREVIEW_DIR, TRANSITIONS, Job, RenderError,
                     build_timeline, cache_info, cleanup_cache, clear_video_cache, merged_settings,
                     apply_my_defaults, builtin_looks, defaults_from, lut_kind, narrate, render, scene_summary,
                     trim_segments)
from .tts import (ELEVEN_MODEL, ELEVEN_MODELS, SAMPLE_RATE, Cancelled, Cartesia, CartesiaError, ElevenLabs,
                  Narrator)

STATIC = Path(__file__).resolve().parent / "static"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_TYPES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}

app = FastAPI(title="StoryCraft")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
APP_DIR = Path(__file__).resolve().parent


def _code_mtime():
    return max(p.stat().st_mtime for p in APP_DIR.glob("*.py"))


STARTED_CODE_MTIME = _code_mtime()


@app.middleware("http")
async def no_stale_static(request, call_next):
    # Always revalidate page files so the browser never mixes old and new versions.
    response = await call_next(request)
    if request.url.path.startswith(("/static/", "/api/audio/")):     # audio files are rewritten in place
        response.headers["Cache-Control"] = "no-cache"
    return response
def make_narrator():
    n = Narrator(CONFIG["kokoro_model"], CONFIG["kokoro_voices"], WORKSPACE / "cache" / "tts",
                 torch_dir=CONFIG.get("kokoro_torch_dir", ""), engine=CONFIG.get("kokoro_engine", "auto"))
    if CONFIG.get("cartesia_api_key"):       # API keys live in config.json: per PC, never shared or committed
        n.cartesia = Cartesia(CONFIG["cartesia_api_key"])
    ElevenLabs.model = CONFIG.get("elevenlabs_model") if CONFIG.get("elevenlabs_model") in ELEVEN_MODELS else ELEVEN_MODEL
    if CONFIG.get("elevenlabs_api_key"):
        n.eleven = ElevenLabs(CONFIG["elevenlabs_api_key"])
    return n


narrator = make_narrator()
jobs = {}
current = {"job": None}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/instance")
def instance():
    """Lets a second start.bat tell this copy apart from other apps (e.g. Mr Sidequests) on the port."""
    return {"app": "StoryCraft", "root": str(ROOT)}


@app.get("/api/status")
def status(refresh_voices: bool = False):
    """refresh_voices (sent once when the page opens) re-reads the online voice lists, so a new
    clone shows up after reloading the page."""
    if refresh_voices:
        for client in (narrator.cartesia, narrator.eleven):
            if client:
                client.voices(refresh=True)
    voices = narrator.voices()
    defaults = json.loads(json.dumps(DEFAULT_SETTINGS))
    kokoro_count = len(voices)
    clouds = {}
    for name, client in (("cartesia", narrator.cartesia), ("elevenlabs", narrator.eleven)):
        clouds[name] = {"configured": bool(client), "error": None, "count": 0}
        if client:
            listed = client.voices()
            clouds[name].update(error=client.error, count=len(listed))
            voices = voices + listed
    clouds["elevenlabs"].update(model=ElevenLabs.model, models=ELEVEN_MODELS)
    # The default voice may be an online one (e.g. your ElevenLabs voice); only replace it if it's gone.
    if voices and defaults["voice"]["voice"] not in {v["id"] for v in voices}:
        defaults["voice"]["voice"] = voices[0]["id"]
    return {"kokoro_error": narrator.check(), "kokoro_engine": narrator.engine_label,
            "ffmpeg": CONFIG.get("ffmpeg") or None, **clouds,
            "kokoro_voice_count": kokoro_count,
            "voices": voices, "defaults": defaults, "motions": MOTIONS, "transitions": TRANSITIONS,
            "looks": builtin_looks(),
            "fonts": list_fonts(),
            "projects_dir": str(PROJECTS_DIR), "config_file": str(ROOT / "config.json"),
            "restart_needed": _code_mtime() > STARTED_CODE_MTIME}


def list_fonts():
    import winreg
    names = set()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[1]):
            name = winreg.EnumValue(key, i)[0]
            name = re.sub(r"\s*\((TrueType|OpenType|All res)\)$", "", name)
            for part in name.split(" & "):
                part = re.sub(r"\s+(Bold|Italic|Bold Italic|Regular|Light|Semibold|SemiBold)$", "", part)
                if part and not part.startswith("@"):
                    names.add(part.strip())
    return sorted(names, key=str.lower)


# ---- images ----------------------------------------------------------------------------------
@app.post("/api/upload")
async def upload(files: list[UploadFile]):
    """Store a copy of each uploaded image in media/ (content-addressed). Originals are untouched."""
    saved = []
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in IMAGE_TYPES:
            raise HTTPException(400, f"{f.filename}: only PNG, JPG and WebP images are supported.")
        data = await f.read()
        digest = hashlib.sha1(data).hexdigest()[:16]
        safe = re.sub(r"[^\w.\- ]", "_", Path(f.filename).name)
        target = MEDIA_DIR / f"{digest}_{safe}"
        if not target.exists():
            target.write_bytes(data)
        saved.append({"path": str(target), "name": Path(f.filename).name})
    return saved


@app.get("/api/image")
def image(path: str):
    p = Path(path)
    if p.suffix.lower() not in IMAGE_TYPES or not p.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(p)


@app.post("/api/check_images")
def check_images(body: dict):
    """Missing images, plus automatic fixes for images that are in this app's media folder
    (e.g. after the app folder was copied to another PC or renamed)."""
    missing, relinked = [], {}
    for p in body.get("paths", []):
        if p and Path(p).is_file():
            continue
        fixed = relink(p)
        if fixed:
            relinked[p] = fixed
        else:
            missing.append(p)
    return {"missing": missing, "relinked": relinked}


def relink(path):
    """Find a moved image: same file name inside this app's media/ folder."""
    if not path:
        return None
    from pathlib import PureWindowsPath
    candidate = MEDIA_DIR / PureWindowsPath(path).name
    return str(candidate) if candidate.is_file() else None


# ---- narration -------------------------------------------------------------------------------
def _require_voice(voice):
    """The chosen voice's engine must be ready: local Kokoro, or Cartesia with a working API key."""
    problem = narrator.check(voice)
    if problem:
        raise HTTPException(500, problem)


def _project_voice(project):
    return merged_settings(project)["voice"]["voice"]


def _require_project_voice(project):
    if not recording.of(project):      # your own recording needs no voice engine
        _require_voice(_project_voice(project))


CLOUD_VOICES = {"cartesia": ("cartesia_api_key", "cartesia", Cartesia),
                "elevenlabs": ("elevenlabs_api_key", "eleven", ElevenLabs)}


@app.post("/api/elevenlabs/model")
def elevenlabs_model(body: dict):
    model = body.get("model")
    if model not in ELEVEN_MODELS:
        raise HTTPException(400, "Unknown ElevenLabs model")
    ElevenLabs.model = CONFIG["elevenlabs_model"] = model
    config_module.save(CONFIG)
    return {"ok": True}


@app.post("/api/{provider}/key")
def cloud_voice_key(provider: str, body: dict):
    """Save (or remove, with an empty key) a Cartesia / ElevenLabs API key after checking that it works."""
    if provider not in CLOUD_VOICES:
        raise HTTPException(404, "Unknown voice service")
    config_key, attr, cls = CLOUD_VOICES[provider]
    key = (body.get("key") or "").strip()
    if key:
        trial = cls(key)
        problem = trial.check()
        if problem:
            raise HTTPException(400, problem)
        CONFIG[config_key] = key
        setattr(narrator, attr, trial)
    else:
        CONFIG.pop(config_key, None)
        setattr(narrator, attr, None)
    config_module.save(CONFIG)
    client = getattr(narrator, attr)
    return {"ok": True, "count": len(client.voices()) if client else 0}


@app.post("/api/scene_audio")
def scene_audio(body: dict):
    """Narrate one scene (cached) so it can be played on its own."""
    project, index = body["project"], int(body["index"])
    _require_project_voice(project)
    scene = project["scenes"][index]
    if recording.of(project):      # a scene's stretch depends on its neighbours: cut them all (fast)
        try:
            return scene_summary(build_timeline(project, narrate(project, narrator, Job("scene"))))[index]
        except (RenderError, recording.RecordingError) as e:
            raise HTTPException(400, str(e))
    if not (scene.get("script") or "").strip():
        raise HTTPException(400, "This scene has no narration script yet.")
    single = {**project, "scenes": [scene]}
    try:
        item = narrate(single, narrator, Job("scene"))[0]
    except (RenderError, CartesiaError) as e:
        raise HTTPException(400, str(e))
    tl = build_timeline(single, [item])
    return scene_summary(tl)[0]


@app.post("/api/test_voice")
def test_voice(body: dict):
    voice, speed = body.get("voice"), float(body.get("speed", 1.0))
    _require_voice(voice)
    text = (body.get("text") or "").strip() or \
        "At 8:43 PM, the world was about to end. Unfortunately, you had absolutely no idea."
    try:
        audio, _, _ = narrator.scene(text, voice, speed, style=body.get("style", "story"))
    except CartesiaError as e:
        raise HTTPException(400, str(e))
    model = ElevenLabs.model if str(voice).startswith("eleven:") else ""
    name = "test_" + hashlib.sha1(f"{voice}{speed}{body.get('style', 'story')}{model}{text}".encode()).hexdigest()[:16] + ".wav"
    sf.write(str(AUDIO_DIR / name), audio, SAMPLE_RATE)
    return {"audio_url": f"/api/audio/{name}", "duration": round(len(audio) / SAMPLE_RATE, 2)}


@app.get("/api/audio/{name}")
def audio_file(name: str):
    p = AUDIO_DIR / Path(name).name
    if not p.is_file():
        raise HTTPException(404, "Audio not found")
    return FileResponse(p, media_type="audio/wav")


# ---- jobs (narrate / preview / export) -------------------------------------------------------
def _run_job(job, project):
    try:
        if job.kind == "transcribe":
            rec = recording.of(project) or {}
            heard = recording.transcribe(rec.get("file", ""), job)
            job.result = {**heard, "parts": recording.make_scenes(heard["words"])}
        elif job.kind == "narrate":
            scenes = project.get("scenes") or []
            if not scenes:
                raise RenderError("Add at least one scene.")
            empty = [str(i) for i, s in enumerate(scenes, 1) if not (s.get("script") or "").strip() and not s.get("image")]
            if empty and not recording.of(project):
                raise RenderError("These scenes are empty (no narration and no image): " + ", ".join(empty))
            tl = build_timeline(project, narrate(project, narrator, job))
            job.result = {"scenes": scene_summary(tl), "duration": round(tl["total"], 3)}
        else:
            preview = job.kind == "preview"
            result = render(project, narrator, job, preview=preview)
            if preview:
                result["url"] = f"/api/preview/{Path(result['path']).name}"
            job.result = result
        job.update("Done", 1.0)
        job.state = "done"
    except Cancelled:
        job.state, job.stage = "cancelled", "Cancelled"
    except (RenderError, CartesiaError, recording.RecordingError) as e:
        job.state, job.error = "error", str(e)
    except Exception as e:  # unexpected: keep the details for the user instead of failing silently
        traceback.print_exc()
        job.state, job.error = "error", f"{type(e).__name__}: {e}"
    finally:
        if job.kind not in ("narrate", "transcribe"):
            trim_segments()


@app.post("/api/jobs")
def start_job(body: dict):
    kind = body.get("kind")
    if kind not in ("narrate", "preview", "export", "transcribe"):
        raise HTTPException(400, "Unknown job")
    _require_project_voice(body["project"])
    running = current["job"]
    if running and running.state == "running":
        raise HTTPException(409, "Another task is still running. Wait for it or cancel it.")
    job = Job(kind)
    jobs[job.id] = job
    current["job"] = job
    threading.Thread(target=_run_job, args=(job, body["project"]), daemon=True).start()
    return job.to_dict()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Unknown job")
    return jobs[job_id].to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if job_id in jobs:
        jobs[job_id].cancel()
    return {"ok": True}


@app.get("/api/cache")
def cache_status():
    return cache_info()


@app.post("/api/cache/clear")
def cache_clear():
    running = current["job"]
    if running and running.state == "running":
        raise HTTPException(409, "Wait for the current task to finish before clearing the cache.")
    clear_video_cache()
    return cache_info()


# ---- series style: characters + art style shared by all projects -----------------------------------
SERIES_FILE = ROOT / "series_style.json"
RECOMMENDED_STYLE = 'Flat 2D webcomic style, like animated storytelling channels on YouTube. Simple shapes and clean dark outlines of even thickness. Flat colors with at most one soft shadow tone; no gradients, textures or glossy highlights. Muted, slightly desaturated natural palette (greys, greens, browns, dusty blues). Plain, even lighting (overcast daylight or ordinary indoor light). Simple backgrounds in the same flat style, everything in focus. Characters are simple cartoon people with round heads, small dot eyes, simple eyebrows and small mouths, wearing plain clothes; emotions are shown with eyebrows, small mouth shapes and body language, never with exaggerated anime faces. Calm, readable poses.'
RECOMMENDED_AVOID = 'realistic humans, photorealism, photographs, 3D render, anime or manga style, painterly or highly detailed rendering, cinematic or dramatic lighting, glowing light sources, light rays, lens flare, bokeh, depth-of-field blur, rain or wet reflections, neon, oversaturated colors, detailed textures, exaggerated anime faces or huge open screaming mouths'
DEFAULT_SERIES = {"style": RECOMMENDED_STYLE, "avoid": RECOMMENDED_AVOID,
                  "style_images": [], "characters": []}


def _fix_paths(paths):
    out = []
    for p in paths or []:
        if p and Path(p).is_file():
            out.append(p)
        elif relink(p):
            out.append(relink(p))
    return out


# ---- art style folder: every image in artstyle/ is a style example; artstyle/rules.txt adds rules ----
ART_TYPES = {".png", ".jpg", ".jpeg", ".webp"}


def _migrate_style_settings():
    """One time: earlier versions kept the art style in settings. Move custom rules into rules.txt and
    old style example images into the folder, so nothing is lost."""
    if not SERIES_FILE.exists():
        return
    try:
        data = json.loads(SERIES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("style_migrated"):
        return
    rules = ARTSTYLE_DIR / "rules.txt"
    lines = []
    style = (data.get("style") or "").strip()
    if style and style != RECOMMENDED_STYLE:
        for sentence in re.findall(r"[^.!?]+[.!?]?", style):
            sentence = sentence.strip()
            if sentence and sentence not in RECOMMENDED_STYLE and "follow the style of the character" not in sentence.lower():
                lines.append(sentence)
    known = {x.strip().lower() for x in RECOMMENDED_AVOID.split(",")} | {"detailed realistic faces"}
    extra = [x.strip() for x in re.split(r"[,;]", data.get("avoid") or "") if x.strip() and x.strip().lower() not in known]
    if extra:
        lines.append("Never draw: " + ", ".join(extra) + ".")
    if lines and not rules.exists():
        rules.write_text("# Extra rules added to every image prompt (one per line). Lines starting with # are ignored.\n"
                         + "\n".join(lines) + "\n", encoding="utf-8")
    if not any(p.suffix.lower() in ART_TYPES for p in ARTSTYLE_DIR.iterdir()):
        for p in _fix_paths(data.get("style_images")):
            shutil.copy2(p, ARTSTYLE_DIR / Path(p).name)
    data["style_migrated"] = True
    SERIES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@app.get("/api/artstyle")
def artstyle():
    _migrate_style_settings()
    images = sorted((p for p in ARTSTYLE_DIR.iterdir() if p.is_file() and p.suffix.lower() in ART_TYPES),
                    key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)])
    rules = _art_lines("rules.txt")
    style, avoid = " ".join(_art_lines("style.txt")), ", ".join(_art_lines("avoid.txt"))
    return {"folder": str(ARTSTYLE_DIR), "images": [str(p) for p in images], "rules": rules,
            "style": style or RECOMMENDED_STYLE, "avoid": avoid or RECOMMENDED_AVOID, "custom_style": bool(style),
            "last_scene": _art_lines("last_scene.txt")}


def _art_lines(name):
    """Non-empty lines of a text file in artstyle/, without # comments. style.txt (the look) and avoid.txt
    (things never to draw) replace the built-in style when present; rules.txt adds rules; last_scene.txt adds rules
    for the final scene only."""
    path = ARTSTYLE_DIR / name
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return [l.strip().rstrip(",") for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


@app.post("/api/artstyle/open")
def artstyle_open():
    os.startfile(str(ARTSTYLE_DIR))
    return {"ok": True}


@app.get("/api/series_style/recommended")
def recommended_series_style():
    return {"style": RECOMMENDED_STYLE, "avoid": RECOMMENDED_AVOID}


@app.get("/api/series_style")
def get_series_style():
    data = dict(DEFAULT_SERIES)
    if SERIES_FILE.exists():
        try:
            data.update(json.loads(SERIES_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    data["style_images"] = _fix_paths(data.get("style_images"))
    for c in data.get("characters", []):
        c["images"] = _fix_paths(c.get("images"))
    return data


@app.post("/api/series_style")
def save_series_style(body: dict):
    clean = {"style": str(body.get("style", ""))[:4000], "avoid": str(body.get("avoid", ""))[:2000],
             "style_images": [str(p) for p in body.get("style_images", [])][:4], "characters": [],
             "style_migrated": bool(body.get("style_migrated", True))}
    # Generous limits: only a scene's own characters (up to 3) go into its prompt, so the list can grow.
    for c in body.get("characters", [])[:200]:
        clean["characters"].append({"id": str(c.get("id", ""))[:40], "name": str(c.get("name", ""))[:80],
                                    "description": str(c.get("description", ""))[:2000], "main": bool(c.get("main")),
                                    "images": [str(p) for p in c.get("images", [])][:2]})
    tmp = SERIES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SERIES_FILE)
    return {"ok": True}


# ---- scene images with ChatGPT (Codex CLI signed in with ChatGPT) ----------------------------------
image_jobs = {}
image_current = {"job": None}


@app.get("/api/imagegen/models")
def imagegen_models(force: bool = False):
    available = imagegen.models(force=force)
    return {"models": available, "chosen": imagegen.chosen_model() or "",
            "default": available[0]["name"] if available else ""}


@app.post("/api/imagegen/model")
def imagegen_set_model(body: dict):
    slug = str(body.get("model") or "")
    if slug and not any(m["slug"] == slug for m in imagegen.models()):
        raise HTTPException(400, "That model isn't offered by your Codex.")
    CONFIG["image_model"] = slug
    config_module.save(CONFIG)
    return {"chosen": slug}


@app.get("/api/imagegen/status")
def imagegen_status(force: bool = False):
    s = imagegen.status(force=force)
    return {k: s[k] for k in ("ready", "version", "message")}


REF_GRID_DIR = WORKSPACE / "cache" / "refgrids"


def _reference_grid(paths):
    """Several character sheets combined into one image (ChatGPT accepts only 5 reference images).
    Cells are filled left to right, top to bottom: 2 per row for up to 4 sheets, else 3."""
    paths = [str(p) for p in paths if p and Path(p).is_file()]
    if len(paths) < 2:
        return paths[0] if paths else None
    REF_GRID_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(json.dumps([[p, Path(p).stat().st_mtime] for p in paths]).encode()).hexdigest()[:20]
    out = REF_GRID_DIR / f"{key}.png"
    if out.exists():
        return str(out)
    cols, w, h, m = (3 if len(paths) > 4 else 2), 768, 480, 8
    args = [CONFIG["ffmpeg"], "-y", "-v", "error"]
    for p in paths:
        args += ["-i", p]
    cells = "".join(f"[{k}:v]scale={w - 2 * m}:{h - 2 * m}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1[c{k}];" for k in range(len(paths)))
    layout = "|".join(f"{(k % cols) * w}_{(k // cols) * h}" for k in range(len(paths)))
    args += ["-filter_complex", cells + "".join(f"[c{k}]" for k in range(len(paths))) +
             f"xstack=inputs={len(paths)}:layout={layout}:fill=white[out]",
             "-map", "[out]", "-frames:v", "1", str(out)]
    r = subprocess.run(args, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not out.exists():
        raise imagegen.ImageError("Could not combine the character sheets: " + (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][:200])
    return str(out)


def _expand_references(refs):
    out = []
    for r in refs or []:
        path = _reference_grid(r.get("grid") or []) if isinstance(r, dict) else r
        if path:
            out.append(path)
    return out


def _run_image_job(job, body):
    try:
        job.update("Starting Codex…", 0.05)
        path = imagegen.generate(body.get("prompt", ""), _expand_references(body.get("references")),
                                 cancelled=job.cancelled, on_progress=lambda m: job.update(m, 0.3))
        job.result = {"path": path, "name": Path(path).name}
        job.update("Done", 1.0)
        job.state = "done"
    except imagegen.Cancelled:
        job.state, job.stage = "cancelled", "Cancelled"
    except imagegen.ImageError as e:
        job.state, job.error = "error", str(e)
    except Exception as e:
        traceback.print_exc()
        job.state, job.error = "error", f"{type(e).__name__}: {e}"


@app.post("/api/imagegen/jobs")
def imagegen_start(body: dict):
    if not (body.get("prompt") or "").strip():
        raise HTTPException(400, "The image prompt is empty.")
    running = image_current["job"]
    if running and running.state == "running":
        raise HTTPException(409, "Another image is still being generated. Wait for it or cancel it.")
    job = Job("image")
    image_jobs[job.id] = job
    image_current["job"] = job
    threading.Thread(target=_run_image_job, args=(job, body), daemon=True).start()
    return job.to_dict()


@app.get("/api/imagegen/jobs/{job_id}")
def imagegen_job(job_id: str):
    if job_id not in image_jobs:
        raise HTTPException(404, "Unknown image job")
    return image_jobs[job_id].to_dict()


@app.post("/api/imagegen/jobs/{job_id}/cancel")
def imagegen_cancel(job_id: str):
    if job_id in image_jobs:
        image_jobs[job_id].cancel()
    return {"ok": True}


@app.get("/api/preview/{name}")
def preview_file(name: str):
    p = PREVIEW_DIR / Path(name).name
    if not p.is_file():
        raise HTTPException(404, "Preview not found")
    return FileResponse(p, media_type="video/mp4")


# ---- projects & dialogs ----------------------------------------------------------------------
@app.post("/api/project/save")
def save_project(body: dict):
    project, path = body["project"], body.get("path")
    if not path or body.get("save_as"):
        name = (project.get("name") or "StoryCraft").strip() or "StoryCraft"
        path = dialogs.ask("save", title="Save project", initialdir=str(PROJECTS_DIR),
                           initialfile=f"{name}.storycraft.json", defaultextension=".json",
                           filetypes=[["StoryCraft project", "*.json"]])
        if not path:
            return {"path": None}
    project = {**project, "version": 1}
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return {"path": str(Path(path))}


@app.post("/api/project/open")
def open_project(body: dict):
    path = body.get("path") or dialogs.ask("open", title="Open project", initialdir=str(PROJECTS_DIR),
                                           filetypes=[["StoryCraft project", "*.json"]])
    if not path:
        return {"path": None}
    try:
        project = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"Could not open project: {e}")
    if not isinstance(project.get("scenes"), list):
        raise HTTPException(400, "This file is not a StoryCraft project.")
    project["settings"] = merged_settings(project)
    missing, relinked = [], 0
    for scene in project["scenes"]:
        image = scene.get("image")
        if image and Path(image).is_file():
            continue
        fixed = relink(image)
        if fixed:
            scene["image"], relinked = fixed, relinked + 1
        else:
            missing.append(image)
    return {"path": path, "project": project, "missing": missing, "relinked": relinked}


@app.post("/api/setup/locate")
def setup_locate(body: dict):
    """Let the user point to Kokoro or FFmpeg when auto-detection failed; saved in config.json."""
    global narrator
    kind = body.get("kind")
    if kind == "ffmpeg":
        path = dialogs.ask("open", title="Locate ffmpeg.exe", filetypes=[["ffmpeg.exe", "ffmpeg.exe"]])
        if not path:
            return {"ok": False}
        probe = Path(path).with_name("ffprobe.exe")
        if not probe.exists():
            raise HTTPException(400, "ffprobe.exe must be in the same folder as ffmpeg.exe.")
        CONFIG.update(ffmpeg=str(Path(path)), ffprobe=str(probe))
    elif kind == "kokoro":
        path = dialogs.ask("open", title="Locate the Kokoro model file",
                           filetypes=[["Kokoro model", "kokoro-v1.0.onnx kokoro-v1_0.pth"], ["All files", "*.*"]])
        if not path:
            return {"ok": False}
        p = Path(path)
        if p.name.endswith(".onnx"):
            voices = p.with_name("voices-v1.0.bin")
            if not voices.exists():
                raise HTTPException(400, "voices-v1.0.bin must be in the same folder as the .onnx model.")
            CONFIG.update(kokoro_engine="onnx", kokoro_model=str(p), kokoro_voices=str(voices), kokoro_torch_dir="")
        elif p.name.endswith(".pth"):
            CONFIG.update(kokoro_engine="torch", kokoro_torch_dir=str(p.parent), kokoro_model="", kokoro_voices="")
        else:
            raise HTTPException(400, "Choose kokoro-v1.0.onnx (ONNX) or kokoro-v1_0.pth (PyTorch / Kokoro-82M).")
        narrator = make_narrator()
    else:
        raise HTTPException(400, "Unknown setup item")
    config_module.save(CONFIG)
    return {"ok": True}


# ---- episodes: "continues from" a previous episode's project file -----------------------------------
def _episode_name(path):
    name = Path(path).name
    for ext in (".storycraft.json", ".sidequest.json", ".json"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


# ---- background music ------------------------------------------------------------------------
@app.post("/api/music/pick")
def music_pick():
    """Choose a music file; a copy is kept in media/ so the project still works if the original moves."""
    path = dialogs.ask("open", title="Choose background music",
                       filetypes=[["Audio", " ".join("*" + e for e in sorted(AUDIO_TYPES))]])
    if not path:
        return {"path": None}
    src = Path(path)
    if src.suffix.lower() not in AUDIO_TYPES:
        raise HTTPException(400, "Choose an audio file: " + ", ".join(sorted(AUDIO_TYPES)))
    data = src.read_bytes()
    safe = re.sub(r"[^\w.\- ]", "_", src.name)
    target = MEDIA_DIR / f"music_{hashlib.sha1(data).hexdigest()[:16]}_{safe}"
    if not target.exists():
        target.write_bytes(data)
    return {"path": str(target), "name": src.name}


@app.post("/api/defaults")
def save_my_defaults(body: dict):
    """Make these settings the starting point of every new project (saved in my_defaults.json)."""
    mine = defaults_from(body.get("settings") or {})
    MY_DEFAULTS_FILE.write_text(json.dumps(mine, indent=2, ensure_ascii=False), encoding="utf-8")
    apply_my_defaults(mine)
    return {"ok": True}


@app.post("/api/defaults/reset")
def reset_my_defaults():
    """Back to StoryCraft's own starting settings."""
    MY_DEFAULTS_FILE.unlink(missing_ok=True)
    apply_my_defaults({})
    return {"ok": True}


@app.post("/api/lut/pick")
def lut_pick():
    """Choose a .cube color grade; a copy is kept in media/ so the project still works if the original moves."""
    path = dialogs.ask("open", title="Choose a color grade (.cube LUT)", filetypes=[["LUT", "*.cube"]])
    if not path:
        return {"path": None}
    src = Path(path)
    if not lut_kind(src):
        raise HTTPException(400, "That file is not a .cube LUT (it has no LUT_3D_SIZE or LUT_1D_SIZE line).")
    data = src.read_bytes()
    safe = re.sub(r"[^\w.\- ]", "_", src.name)
    target = MEDIA_DIR / f"lut_{hashlib.sha1(data).hexdigest()[:16]}_{safe}"
    if not target.exists():
        target.write_bytes(data)
    return {"path": str(target), "name": src.stem}


@app.post("/api/recording/pick")
def recording_pick():
    """Choose your voice-over; a copy is kept in media/ so the project still works if the original moves."""
    path = dialogs.ask("open", title="Choose your recording",
                       filetypes=[["Audio", " ".join("*" + e for e in sorted(AUDIO_TYPES))]])
    if not path:
        return {"path": None}
    src = Path(path)
    if src.suffix.lower() not in AUDIO_TYPES:
        raise HTTPException(400, "Choose an audio file: " + ", ".join(sorted(AUDIO_TYPES)))
    data = src.read_bytes()
    safe = re.sub(r"[^\w.\- ]", "_", src.name)
    target = MEDIA_DIR / f"recording_{hashlib.sha1(data).hexdigest()[:16]}_{safe}"
    if not target.exists():
        target.write_bytes(data)
    try:
        duration = len(recording.decoded(str(target))) / SAMPLE_RATE
    except recording.RecordingError as e:
        raise HTTPException(400, str(e))
    return {"path": str(target), "name": src.name, "duration": round(duration, 3)}


@app.get("/api/music")
def music_file(path: str):
    p = Path(path)
    if p.suffix.lower() not in AUDIO_TYPES or not p.is_file():
        raise HTTPException(404, "Music file not found")
    return FileResponse(p)


@app.post("/api/episode/pick")
def episode_pick(body: dict):
    path = dialogs.ask("open", title="Choose the previous episode", initialdir=str(PROJECTS_DIR),
                       filetypes=[["StoryCraft project", "*.json"]])
    if not path:
        return {"path": None}
    if body.get("current") and Path(path).resolve() == Path(body["current"]).resolve():
        raise HTTPException(400, "That's this episode itself. Choose the episode that comes before it.")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert isinstance(data.get("scenes"), list)
    except (OSError, ValueError, AssertionError):
        raise HTTPException(400, "That file is not a StoryCraft project.")
    return {"path": str(Path(path)), "name": _episode_name(path)}


@app.post("/api/episode/context")
def episode_context(body: dict):
    """The previous episodes (newest first, up to 3 back, following each one's own "continues from"):
    their narration and images, for continuity of story, place and time."""
    chain, seen, path = [], set(), body.get("path")
    while path and len(chain) < 3:
        key = str(Path(path).resolve()).lower()
        if key in seen:
            break
        seen.add(key)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if not chain:
                raise HTTPException(404, f"The previous episode file was not found: {Path(path).name}")
            break
        scenes = []
        for sc in data.get("scenes", []):
            img = sc.get("image") or ""
            if img and not Path(img).is_file():
                img = relink(img) or ""
            scenes.append({"script": sc.get("script") or "", "image": img})
        story = (data.get("settings") or {}).get("story") or {}
        chain.append({"name": _episode_name(path), "path": str(path), "scenes": scenes, "world": story.get("world") or ""})
        path = story.get("continues_from") or ""
    return {"chain": chain}


@app.post("/api/dialog/folder")
def choose_folder(body: dict):
    return {"path": dialogs.ask("folder", title="Choose output folder",
                                initialdir=body.get("initial") or str(ROOT)) or None}


@app.post("/api/reveal")
def reveal(body: dict):
    p = Path(body.get("path", ""))
    if p.is_file():
        subprocess.Popen(["explorer", "/select,", str(p)])
    elif p.is_dir():
        os.startfile(str(p))
    return {"ok": True}


def _port_owner(port):
    """'free', 'self' (this StoryCraft folder is already serving it) or 'other'."""
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return "free"
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/instance", timeout=3) as r:
            info = json.loads(r.read().decode("utf-8"))
        same = os.path.normcase(os.path.abspath(info.get("root", ""))) == os.path.normcase(str(ROOT))
        return "self" if same else "other"
    except (OSError, ValueError):
        return "other"


def main():
    port = int(CONFIG.get("port", 8766))
    for candidate in range(port, port + 20):
        owner = _port_owner(candidate)
        if owner == "other":
            continue                                    # another app (e.g. Mr Sidequests): skip it
        port = candidate
        break
    else:
        print(f"No free port found between {port} and {port + 19}.")
        return
    if port != int(CONFIG.get("port", 8766)):
        print(f"Port {CONFIG.get('port')} is used by another app, using {port} instead.")
    url = f"http://127.0.0.1:{port}"
    if owner == "self":                                 # already running: just open it
        print(f"StoryCraft is already running at {url}")
        if not os.environ.get("STORYCRAFT_NO_BROWSER"):
            webbrowser.open(url)
        return
    cleanup_cache()
    if not os.environ.get("STORYCRAFT_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    import uvicorn
    print(f"StoryCraft is running at {url}  (close this window to stop)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
