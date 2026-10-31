"""Your own voice-over instead of an AI voice.

The recording (voice, often with music under it) plays unchanged from start to end as the video's
soundtrack. Whisper (faster-whisper, already on this PC) writes down every word with its time;
that turns the recording into scenes (one per sentence, or more with a slow fade) and times the captions. Each scene keeps
`rec_start`, the second in the recording where it begins; it ends where the next scene begins.
"""
import hashlib

import re
import subprocess

import numpy as np
import soundfile as sf

from .config import CONFIG, WORKSPACE
from .captions import build_captions
from .tts import SAMPLE_RATE, _align_words

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DECODED_DIR = WORKSPACE / "cache" / "recordings"
DECODED_DIR.mkdir(parents=True, exist_ok=True)
WHISPER_MODELS = ("small.en", "tiny.en")      # already downloaded on this PC; small.en is the accurate one
MIN_SCENE, MAX_SCENE = 2.0, 8.0               # seconds: shorter sentences join the next, longer ones are split
SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")
SOFT_END = re.compile(r"[,;:—–-][\"'”’)\]]*$")


class RecordingError(RuntimeError):
    pass


def of(project):
    """The project's recording, or None when it uses an AI voice."""
    rec = project.get("recording") or {}
    return rec if rec.get("file") else None


def _key(path):
    from pathlib import Path
    st = Path(path).stat()
    return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


def decoded(path):
    """The recording as mono float audio at SAMPLE_RATE (cached), for slicing per scene."""
    from pathlib import Path
    if not Path(path).is_file():
        raise RecordingError(f"Your recording was not found ({Path(path).name}). Choose it again in Settings → Voice.")
    wav = DECODED_DIR / f"{_key(path)}.wav"
    if not wav.exists():
        tmp = wav.with_suffix(".tmp.wav")
        proc = subprocess.run([CONFIG["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(path),
                               "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", str(tmp)],
                              capture_output=True, text=True, creationflags=NO_WINDOW)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RecordingError("Could not read your recording: " + (proc.stderr.strip()[-300:] or "unknown error"))
        tmp.replace(wav)
    audio, _ = sf.read(str(wav), dtype="float32")
    return audio


def transcribe(path, job):
    """Every spoken word with its time: {"duration", "words": [[text, start, end], ...]}."""
    duration = len(decoded(path)) / SAMPLE_RATE
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RecordingError("Whisper (faster-whisper) is not installed in this Python.") from None
    job.update("Loading Whisper…", 0.02)
    model = None
    for name in WHISPER_MODELS:
        try:
            model = WhisperModel(name, device="cpu", compute_type="int8", local_files_only=True)
            break
        except Exception:      # not downloaded: try the next one
            continue
    if model is None:
        raise RecordingError("No Whisper model was found on this PC (small.en or tiny.en).")
    # Without a punctuated prompt Whisper often leaves out full stops, and scenes are cut at sentence ends.
    segments, _ = model.transcribe(str(path), language="en", word_timestamps=True, beam_size=5,
                                   condition_on_previous_text=False,
                                   initial_prompt="Hello. This is a story, told with full punctuation.")
    words = []
    for seg in segments:
        if job.cancelled():
            from .tts import Cancelled
            raise Cancelled()
        job.update(f"Listening to your recording… {seg.end:.0f}s of {duration:.0f}s", 0.05 + 0.9 * min(1, seg.end / (duration or 1)))
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append([text, round(float(w.start), 3), round(float(max(w.start, w.end)), 3)])
    if not words:
        raise RecordingError("No speech was found in the recording.")
    return {"duration": round(duration, 3), "words": refine_words(words, decoded(path))}


FRAME = 0.02            # seconds per loudness frame
CAPTION_PAUSE = 0.35    # a silence this long between two words always starts a new caption


def refine_words(words, audio):
    """Whisper often gives a word the silence before it ("and" 8.08–9.64 s when it is spoken at 9.6 s),
    so its caption would appear while nothing is said. For a word that lasts far longer than it
    takes to say, find where the voice really is in that stretch: the last loud part, followed back
    to where it starts (short dips inside a word are allowed). Works with music under the voice,
    because it compares loudness within the word's own stretch."""
    hop = int(SAMPLE_RATE * FRAME)
    n = len(audio) // hop
    if not n:
        return words
    rms = np.sqrt((audio[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    floor = float(np.percentile(rms, 15))
    out = []
    for text, s, e in words:
        expected = 0.1 + 0.06 * len(re.sub(r"\W", "", text))
        a, b = int(s / FRAME), min(n, int(np.ceil(e / FRAME)))
        if e - s > expected * 1.6 + 0.2 and b - a > 2:
            span = rms[a:b]
            peak = float(span.max())
            if peak > floor * 1.6:          # there is a voice in this stretch
                last = a + int(np.flatnonzero(span >= 0.5 * peak)[-1])
                first, quiet = last, 0
                while first - 1 >= a and quiet <= 4:
                    first -= 1
                    quiet = quiet + 1 if rms[first] < 0.5 * peak else 0
                first += quiet
                s, e = round(max(s, first * FRAME), 3), round(min(e, max((last + 1) * FRAME, first * FRAME + 0.1)), 3)
        out.append([text, s, e])
    return out


MIN_SCENES = 4      # joining sentences for a slow fade never leaves fewer scenes than this


def scenes_for(words, duration, transition):
    """make_scenes with the longest minimum scene the transition asks for that still gives
    MIN_SCENES scenes (a short recording may give fewer)."""
    want = min_scene_for(transition)
    parts = make_scenes(words, duration, want)
    while len(parts) < MIN_SCENES and want > MIN_SCENE:
        want = max(MIN_SCENE, want - 0.25)
        parts = make_scenes(words, duration, want)
    return parts


def min_scene_for(transition):
    """Shortest scene on screen: with a fade, a scene must stay long enough to be seen between its
    fade in and fade out (3x the fade), so slow fades never make the picture blink every few seconds."""
    if not transition or transition.get("type") == "cut":
        return MIN_SCENE
    return max(MIN_SCENE, 3 * float(transition.get("duration") or 0))


def make_scenes(words, duration=None, min_scene=MIN_SCENE):
    """One scene per sentence: short ones join the next, long ones split at a pause.
    A scene counts as short when it is on screen (from its first word to the next scene's first word)
    for less than min_scene. Returns [{"script", "start"}]; a scene starts halfway through the pause
    before its first word."""
    max_scene = max(MAX_SCENE, 2 * min_scene)
    sentences, cur = [], []
    for w in words:
        cur.append(w)
        if SENTENCE_END.search(w[0]):
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)
    span = lambda ws: ws[-1][2] - ws[0][1]      # noqa: E731

    def split(ws):
        if span(ws) <= max_scene or len(ws) < 4:
            return [ws]
        mid = (ws[0][1] + ws[-1][2]) / 2
        # Only cuts that leave both parts long enough (a shorter part would be joined back right away).
        fits = [k for k in range(1, len(ws)) if span(ws[:k]) >= min_scene and span(ws[k:]) >= min_scene]
        if not fits:
            return [ws]
        # Best cut: after a comma near the middle; otherwise the longest silence between two words.
        cuts = [k for k in fits if SOFT_END.search(ws[k - 1][0])]
        k = min(cuts, key=lambda k: abs(ws[k][1] - mid)) if cuts else \
            max(fits, key=lambda k: ws[k][1] - ws[k - 1][2] - abs(ws[k][1] - mid) * 0.05)
        return split(ws[:k]) + split(ws[k:])

    pieces = [p for s in sentences for p in split(s)]
    end = max(duration or 0, words[-1][2]) if words else 0
    merged = []
    for p in pieces:
        if merged and p[0][1] - merged[-1][0][1] < min_scene:    # the last scene, up to where p starts
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    if len(merged) > 1 and end - merged[-1][0][1] < min_scene:   # a very short last line joins the one before
        merged[-2:] = [merged[-2] + merged[-1]]
    out = []
    for i, ws in enumerate(merged):
        start = 0.0 if i == 0 else round((merged[i - 1][-1][2] + ws[0][1]) / 2, 3)
        out.append({"script": " ".join(w[0] for w in ws), "start": start})
    return out


def bounds(scenes, duration):
    """(start, end) in the recording for every scene. A missing or out-of-order rec_start (a scene
    added or moved by hand) is spread evenly between its neighbours. Same rule as the page."""
    n = len(scenes)
    starts = [None] * n
    last = 0.0
    for i, sc in enumerate(scenes):
        v = sc.get("rec_start")
        if i == 0:
            starts[i] = 0.0
        elif isinstance(v, (int, float)) and last < v < duration:
            starts[i] = last = float(v)
    i = 0
    while i < n:
        if starts[i] is None:
            p = i - 1
            q = i
            while q < n and starts[q] is None:
                q += 1
            a, b = starts[p], (starts[q] if q < n else duration)
            for k in range(i, q):
                starts[k] = a + (b - a) * (k - p) / (q - p)
            i = q
        i += 1
    return [(starts[i], starts[i + 1] if i + 1 < n else duration) for i in range(n)]


def scene_captions(script, words, start, end, words_per_caption):
    """Caption events (seconds from the scene start) for this scene's script, timed by the words
    Whisper heard between start and end. The captions always show the script as typed."""
    ours = (script or "").split()
    if not ours:
        return []
    heard = [w for w in words if start <= (w[1] + w[2]) / 2 < end]
    # Times relative to the first heard word, so if the text no longer matches (edited a lot) the
    # words are spread over the time actually spoken, not over the silence before it.
    base = heard[0][1] if heard else start
    total = (heard[-1][2] - base) if heard else end - start
    times = _align_words(ours, [w[0] for w in heard], [w[1] - base for w in heard], [w[2] - base for w in heard],
                         max(0.1, total))
    shift = base - start
    times = [(a + shift, b + shift) for a, b in times]
    # A pause in the voice always ends a caption, so a caption never shows words not yet spoken.
    events, run = [], [0]
    for k in range(1, len(ours) + 1):
        if k == len(ours) or times[k][0] - times[k - 1][1] > CAPTION_PAUSE:
            events += build_captions([ours[j] for j in run], [times[j] for j in run], words_per_caption)
            run = []
        run.append(k)
    return events
