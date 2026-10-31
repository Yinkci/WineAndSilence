"""Narration with a local Kokoro installation: kokoro-onnx (ONNX) or `kokoro` (PyTorch,
hexgrad/Kokoro-82M) — whichever this PC has.

ONNX: the session is called directly so we can read the model's per-token `duration` output.
PyTorch: KPipeline returns word tokens with start/end timestamps from the same durations.
That gives exact word timings for subtitles straight from the speech model (no speech
recognition needed). Each sentence is synthesized in one pass so prosody stays natural, and
cached on disk keyed by (text, voice, speed): editing captions, images or other scenes never
regenerates speech.
"""
import hashlib
import json
import os
import re
import threading
import zipfile
from pathlib import Path

import numpy as np

from .captions import build_captions, split_sentences

SAMPLE_RATE = 24000
FRAME_SAMPLES = 600       # one Kokoro duration unit = 600 samples (25 ms)
MAX_TOKENS = 480          # model context is 510 incl. padding; leave headroom
CACHE_VERSION = 1
PUNCTUATION = ';:,.!?¡¿—…"«»“”()[] '

LANGUAGES = {
    "a": ("American English", "en-us"), "b": ("British English", "en-gb"),
    "e": ("Spanish", "es"), "f": ("French", "fr-fr"), "h": ("Hindi", "hi"),
    "i": ("Italian", "it"), "j": ("Japanese", "ja"), "p": ("Brazilian Portuguese", "pt-br"),
    "z": ("Mandarin Chinese", "cmn"),
}


# Voices in hexgrad/Kokoro-82M v1.0 (used when the local folder doesn't list voices/*.pt).
TORCH_VOICES = ("af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky "
                "am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa bf_alice bf_emma "
                "bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis ef_dora em_alex em_santa ff_siwis "
                "hf_alpha hf_beta hm_omega hm_psi if_sara im_nicola jf_alpha jf_gongitsune jf_nezumi jf_tebukuro "
                "jm_kumo pf_dora pm_alex pm_santa zf_xiaobei zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi "
                "zm_yunxia zm_yunyang").split()


class Cancelled(Exception):
    pass


# ---- Cartesia (cloud voices) ---------------------------------------------------------------------
# A Cartesia voice is stored in the project as "cartesia:<voice id>", so caching, narration and
# export treat it like any other voice. Word timestamps come from the API, like Kokoro's durations.
CARTESIA_URL = "https://api.cartesia.ai"
CARTESIA_VERSION = "2026-08-14"
CARTESIA_MODEL = "sonic-3.6"
CARTESIA_PREFIX = "cartesia:"
# Reading styles (settings.voice.style): "story" lets the model choose its own delivery;
# "poem-<name>" reads line by line, slower, with its own pauses after each line and verse, and asks
# Cartesia for an emotion (English only, beta; on its own it is too subtle to hear, hence the pacing).
POEM_STYLES = {   # emotion, speed factor, seconds after a line, seconds after a verse (blank line)
    "calm":     {"emotion": "calm",     "speed": 0.88, "line": 0.6, "verse": 1.2},
    "peaceful": {"emotion": "peaceful", "speed": 0.84, "line": 0.75, "verse": 1.4},
    "serene":   {"emotion": "serene",   "speed": 0.80, "line": 0.9, "verse": 1.6},
    "sad":      {"emotion": "sad",      "speed": 0.74, "line": 1.0, "verse": 1.8},
}
CARTESIA_LANGUAGES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
                      "pt": "Portuguese", "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "hi": "Hindi",
                      "nl": "Dutch", "pl": "Polish", "ru": "Russian", "sv": "Swedish", "tr": "Turkish"}


def parse_style(style):
    """settings.voice.style -> POEM_STYLES entry, or None for storytelling."""
    style = str(style or "story")
    if not style.startswith("poem"):
        return None
    return POEM_STYLES.get(style.partition("-")[2], POEM_STYLES["calm"])


def is_cartesia(voice):
    return str(voice or "").startswith(CARTESIA_PREFIX)


class CartesiaError(RuntimeError):
    pass


class Cartesia:
    def __init__(self, api_key):
        self.api_key = (api_key or "").strip()
        self._voices = None
        self.error = None

    def _open(self, method, path, body=None, timeout=60):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(CARTESIA_URL + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Cartesia-Version": CARTESIA_VERSION,
                                              "Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise CartesiaError("Cartesia rejected the API key. Check it in Settings → Voice.") from None
            if e.code == 402:
                raise CartesiaError("Your Cartesia credits have run out.") from None
            raise CartesiaError(f"Cartesia error {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise CartesiaError(f"Can't reach Cartesia ({getattr(e, 'reason', e)}). Check your internet connection.") from None

    def voices(self, refresh=False):
        """Your own voices (clones) first, then the public ones (up to 3000), grouped by language."""
        if self._voices is not None and not refresh:
            return self._voices
        out, seen = [], set()
        try:
            for owner_only in (True, False):
                after = None
                for _ in range(30):
                    query = ("/voices?limit=100" + ("&is_owner=true" if owner_only else "")
                             + (f"&starting_after={after}" if after else ""))
                    with self._open("GET", query, timeout=20) as r:
                        page = json.loads(r.read())
                    self._add_voices(out, seen, page.get("data") or [])
                    after = page.get("next_page")
                    if not page.get("has_more") or not after:
                        break
            self.error = None
        except CartesiaError as e:
            self.error = str(e)
            return []
        out.sort(key=lambda v: (not v["mine"], v["language"] != "Cartesia · English", v["language"], v["name"].lower()))
        self._voices = out
        return out

    @staticmethod
    def _add_voices(out, seen, data):
        for v in data:
            if v["id"] in seen:
                continue
            seen.add(v["id"])
            code = (v.get("language") or "en").split("-")[0].split("_")[0]
            gender = {"masculine": "Male", "feminine": "Female"}.get(v.get("gender"), "")
            lang = CARTESIA_LANGUAGES.get(code, code.upper())
            out.append({"id": CARTESIA_PREFIX + v["id"], "language": f"Cartesia · {lang}",
                        "label": f"{v.get('name') or v['id']} — Cartesia {lang}{', ' + gender if gender else ''}",
                        "name": v.get("name") or v["id"], "lang_code": code,
                        "mine": bool(v.get("is_owner"))})

    def check(self):
        if not self.api_key:
            return "Add your Cartesia API key in Settings → Voice to use Cartesia voices."
        self.voices()
        return self.error

    def synth(self, text, voice, speed, emotion=None):
        """One sentence. Returns (audio float32 @ SAMPLE_RATE, words, starts, ends) from the API."""
        body = {"model_id": CARTESIA_MODEL, "transcript": text, "voice": {"id": voice[len(CARTESIA_PREFIX):]},
                "output_format": {"container": "raw", "encoding": "pcm_f32le", "sample_rate": SAMPLE_RATE},
                "add_timestamps": True,
                "generation_config": {"speed": round(min(1.5, max(0.6, float(speed))), 2)}}
        if emotion:
            body["generation_config"]["emotion"] = emotion
        code = next((v["lang_code"] for v in (self._voices or []) if v["id"] == voice), None)
        if code in CARTESIA_LANGUAGES:
            body["language"] = code
        import base64
        chunks, words, starts, ends = [], [], [], []
        with self._open("POST", "/tts/sse", body, timeout=120) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip() or "{}")
                kind = event.get("type")
                if kind == "chunk" and event.get("data"):
                    chunks.append(base64.b64decode(event["data"]))
                elif kind == "timestamps":
                    ts = event.get("word_timestamps") or {}
                    words += ts.get("words") or []
                    starts += ts.get("start") or []
                    ends += ts.get("end") or []
                elif kind == "error":
                    raise CartesiaError(f"Cartesia: {event.get('message') or event.get('title') or 'generation failed'}")
                elif kind == "done":
                    break
        audio = np.frombuffer(b"".join(chunks), dtype=np.float32).copy()
        if not len(audio):
            raise CartesiaError("Cartesia returned no audio for: " + text[:60])
        return audio, words, starts, ends


def _align_words(words, api_words, starts, ends, total):
    """Map the API's word timings onto our own words (which keep their punctuation)."""
    norm = lambda w: re.sub(r"[^\w']", "", w.lower())  # noqa: E731
    if api_words and len(api_words) == len(words) and len(starts) == len(ends) == len(words):
        return [(float(a), float(max(a, b))) for a, b in zip(starts, ends)]
    times, j = [], 0
    for w in words:                           # walk both lists, joining API words until they match ours
        target, got, s = norm(w), "", None
        while j < len(api_words) and (not target or len(got) < len(target)):
            s = starts[j] if s is None else s
            got += norm(api_words[j])
            e = ends[j]
            j += 1
        if s is None or got != target:
            break
        times.append((float(s), float(max(s, e))))
    if len(times) == len(words):
        return times
    weights = np.cumsum([0] + [len(w) + 1 for w in words]) / (sum(len(w) + 1 for w in words) or 1)
    return [(float(total * weights[i]), float(total * weights[i + 1])) for i in range(len(words))]   # spread by length


def _has_module(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---- ElevenLabs (cloud voices) -------------------------------------------------------------------
# Stored as "eleven:<voice id>". The with-timestamps endpoint returns per-character times, which we
# turn into per-word times for the subtitles.
ELEVEN_URL = "https://api.elevenlabs.io"
ELEVEN_MODEL = "eleven_v4"          # the model the ElevenLabs website uses by default
ELEVEN_MODELS = {"eleven_v4": "Eleven v4 — best quality (the website's first take)",
                 "eleven_v4_exp": "Eleven v4 experimental (the website's second take)",
                 "eleven_v4_turbo": "Eleven v4 Turbo — faster, cheaper",
                 "eleven_multilingual_v2": "Multilingual v2 — the older model"}
ELEVEN_PREFIX = "eleven:"


def is_eleven(voice):
    return str(voice or "").startswith(ELEVEN_PREFIX)


class ElevenLabs:
    name = "ElevenLabs"
    model = ELEVEN_MODEL      # set from config.json (elevenlabs_model) by the app

    def __init__(self, api_key):
        self.api_key = (api_key or "").strip()
        self._voices = None
        self.error = None

    def _open(self, method, path, body=None, timeout=60):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(ELEVEN_URL + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"xi-api-key": self.api_key, "Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 401:
                if "quota" in detail.lower() or "credit" in detail.lower():
                    raise CartesiaError("Your ElevenLabs credits have run out.") from None
                raise CartesiaError("ElevenLabs rejected the API key (or it lacks the text-to-speech / voices "
                                    "permission). Check it in Settings → Voice.") from None
            try:      # ElevenLabs explains itself in {"detail": {"status": ..., "message": ...}}
                info = json.loads(detail).get("detail")
                reason = info.get("message") if isinstance(info, dict) else str(info or "")
                status = info.get("status") if isinstance(info, dict) else ""
            except (ValueError, AttributeError):
                reason, status = detail, ""
            if e.code == 402 and status == "paid_plan_required":
                raise CartesiaError("This ElevenLabs voice needs a paid plan: free accounts can't use Voice Library "
                                    "voices through the API. Pick a voice from \"My voices\" or one of ElevenLabs' "
                                    f"default voices instead. (ElevenLabs: {reason})") from None
            raise CartesiaError(f"ElevenLabs error {e.code}: {reason or detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise CartesiaError(f"Can't reach ElevenLabs ({getattr(e, 'reason', e)}). Check your internet connection.") from None

    def voices(self, refresh=False):
        if self._voices is not None and not refresh:
            return self._voices
        out, token = [], None
        try:
            for _ in range(10):
                query = "/v2/voices?page_size=100&include_total_count=false" + (f"&next_page_token={token}" if token else "")
                with self._open("GET", query, timeout=20) as r:
                    page = json.loads(r.read())
                for v in page.get("voices") or []:
                    labels = v.get("labels") or {}
                    gender = (labels.get("gender") or "").title()
                    accent = (labels.get("accent") or "").title()
                    # "professional" voices in your list are Voice Library voices you added: the API only allows
                    # them on paid plans. "premade" (default) voices work on every plan.
                    cat = v.get("category")
                    mine = cat in ("cloned", "generated")
                    group = ("ElevenLabs · My voices" if mine else "ElevenLabs · Default voices (any plan)" if cat == "premade"
                             else "ElevenLabs · Voice Library (paid plan needed)")
                    out.append({"id": ELEVEN_PREFIX + v["voice_id"], "language": group, "name": v.get("name") or v["voice_id"],
                                "label": f"{v.get('name')} — ElevenLabs{', ' + gender if gender else ''}{', ' + accent if accent else ''}",
                                "detail": ", ".join(x for x in (gender, accent) if x), "mine": mine})
                token = page.get("next_page_token")
                if not page.get("has_more") or not token:
                    break
            self.error = None
        except CartesiaError as e:
            self.error = str(e)
            return []
        order = {"ElevenLabs · My voices": 0, "ElevenLabs · Default voices (any plan)": 1}
        out.sort(key=lambda v: (order.get(v["language"], 2), v["name"].lower()))
        self._voices = out
        return out

    def check(self):
        if not self.api_key:
            return "Add your ElevenLabs API key in Settings → Voice to use ElevenLabs voices."
        self.voices()
        return self.error

    def synth_words(self, text, words, voice, speed, context=None):
        """One sentence → (audio float32 @ SAMPLE_RATE, [(start, end)] per word).
        context = (previous sentence, next sentence) so the sentences flow like one reading."""
        import base64
        body = {"text": text, "model_id": self.model,
                "voice_settings": {"speed": round(min(1.2, max(0.7, float(speed))), 2)}}
        if context and context[0]:
            body["previous_text"] = context[0]
        if context and context[1]:
            body["next_text"] = context[1]
        with self._open("POST", f"/v1/text-to-speech/{voice[len(ELEVEN_PREFIX):]}/with-timestamps"
                                f"?output_format=pcm_{SAMPLE_RATE}", body, timeout=120) as r:
            data = json.loads(r.read())
        pcm = base64.b64decode(data.get("audio_base64") or "")
        audio = (np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0)
        if not len(audio):
            raise CartesiaError("ElevenLabs returned no audio for: " + text[:60])
        al = data.get("alignment") or {}
        chars, st, en = al.get("characters") or [], al.get("character_start_times_seconds") or [], \
            al.get("character_end_times_seconds") or []
        total = len(audio) / SAMPLE_RATE
        if len(chars) == len(text) == len(st) == len(en):
            spans = [(m.start(), m.end() - 1) for m in re.finditer(r"\S+", text)]
            return audio, [(float(st[a]), float(max(st[a], en[b]))) for a, b in spans]
        # Character list doesn't line up with our text: rebuild words from it and align by spelling.
        api_words, starts, ends, cur, s = [], [], [], "", None
        for ch, a, b in zip(chars, st, en):
            if ch.isspace():
                if cur:
                    api_words.append(cur); starts.append(s); ends.append(prev_end)
                cur, s = "", None
            else:
                cur += ch; s = a if s is None else s; prev_end = b
        if cur:
            api_words.append(cur); starts.append(s); ends.append(prev_end)
        return audio, _align_words(words, api_words, starts, ends, total)


class Narrator:
    def __init__(self, model_path, voices_path, cache_dir, torch_dir="", engine="auto"):
        self.model_path = model_path
        self.voices_path = voices_path
        self.torch_dir = torch_dir
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._kokoro = None
        self._tmodel = None
        self._pipes = {}
        self._lock = threading.Lock()
        self.engine = self._pick_engine(engine)
        self.cartesia = None          # set by the app when a Cartesia API key is saved
        self.eleven = None            # set by the app when an ElevenLabs API key is saved

    # ---- setup -------------------------------------------------------------------------------
    def _onnx_ready(self):
        return (self.model_path and Path(self.model_path).exists() and self.voices_path
                and Path(self.voices_path).exists() and _has_module("kokoro_onnx"))

    def _torch_ready(self):
        return _has_module("kokoro") and _has_module("torch")

    def _pick_engine(self, engine):
        if engine == "onnx" or (engine == "auto" and self._onnx_ready()):
            return "onnx"
        if engine == "torch" or (engine == "auto" and self._torch_ready()):
            return "torch"
        return "onnx" if self.model_path else ("torch" if self.torch_dir else None)

    @property
    def engine_label(self):
        return {"onnx": "Kokoro (ONNX)", "torch": "Kokoro (PyTorch)"}.get(self.engine, "Kokoro")

    def check(self, voice=None):
        """Return None when the voice's engine (Kokoro by default) is usable, otherwise a human-readable problem."""
        if is_cartesia(voice):
            return self.cartesia.check() if self.cartesia else "Add your Cartesia API key in Settings → Voice to use Cartesia voices."
        if is_eleven(voice):
            return self.eleven.check() if self.eleven else "Add your ElevenLabs API key in Settings → Voice to use ElevenLabs voices."
        if self.engine == "torch":
            if not _has_module("kokoro"):
                return "The Kokoro Python package (pip install kokoro) is not installed in this Python."
            if not _has_module("torch"):
                return "PyTorch is not installed in this Python (the kokoro package needs it)."
            if self.torch_dir and not (Path(self.torch_dir) / "kokoro-v1_0.pth").exists():
                return f"kokoro-v1_0.pth was not found in {self.torch_dir}."
            return None
        if self.engine is None:
            return ("No Kokoro installation was found. Install Kokoro (hexgrad/Kokoro-82M with the `kokoro` "
                    "package, or kokoro-onnx), or click Locate Kokoro to point to its model file.")
        if not self.model_path or not Path(self.model_path).exists():
            return "Kokoro model (kokoro-v1.0.onnx) not found. Click Locate Kokoro to choose it."
        if not self.voices_path or not Path(self.voices_path).exists():
            return "Kokoro voices (voices-v1.0.bin) not found next to the model file."
        if not _has_module("kokoro_onnx"):
            return "The kokoro-onnx package is not installed in this Python."
        return None

    def voices(self):
        if self.engine == "torch":
            names = []
            if self.torch_dir and (Path(self.torch_dir) / "voices").is_dir():
                names = sorted(f.stem for f in (Path(self.torch_dir) / "voices").glob("*.pt"))
            return self._voice_list(names or TORCH_VOICES)
        if not self.voices_path or not Path(self.voices_path).exists():
            return []
        with zipfile.ZipFile(self.voices_path) as archive:
            names = sorted(Path(n).stem for n in archive.namelist() if n.endswith(".npy"))
        return self._voice_list(names)

    @staticmethod
    def _voice_list(names):
        result = []
        for name in names:
            if name[0] not in LANGUAGES:
                continue
            language = LANGUAGES[name[0]][0]
            gender = "Female" if name[1] == "f" else "Male"
            result.append({"id": name, "label": f"{name[3:].title()} — {language}, {gender}",
                           "language": language})
        return result

    def _model(self):
        if self._kokoro is None:
            from kokoro_onnx import Kokoro
            self._kokoro = Kokoro(self.model_path, self.voices_path)
        return self._kokoro

    # ---- synthesis ---------------------------------------------------------------------------
    def _phonemes_per_word(self, words, lang):
        """Phonemes for each word. Prefer the whole-sentence phonemization (context-aware) and
        split it back into words; fall back to word-by-word if the word counts don't line up."""
        import phonemizer
        vocab = self._model().tokenizer.vocab
        raw = phonemizer.phonemize([" ".join(words), *words], language=lang,
                                   preserve_punctuation=True, with_stress=True)
        clean = ["".join(ch for ch in p if ch in vocab).strip() for p in raw]
        sentence, per_word = clean[0].split(), clean[1:]
        counts = [len(p.split()) for p in per_word]
        if sum(counts) != len(sentence):
            return per_word
        out, k = [], 0
        for c in counts:
            out.append(" ".join(sentence[k:k + c]))
            k += c
        return out

    def _run(self, word_phonemes, voice, speed):
        """Synthesize one group of words. Returns (audio, [(start, end)] seconds per word)."""
        kokoro = self._model()
        vocab = kokoro.tokenizer.vocab
        tokenize = lambda s: [vocab[c] for c in s if c in vocab]  # noqa: E731
        ids, spans = [], []
        for pw in word_phonemes:
            if ids and pw:
                ids += tokenize(" ")
            start = len(ids)
            ids += tokenize(pw)
            core_end = start + len(tokenize(pw.rstrip(PUNCTUATION)))
            spans.append((start, core_end))
        style = np.asarray(kokoro.voices[voice][len(ids)], dtype=np.float32).reshape(1, -1)
        names = [i.name for i in kokoro.sess.get_inputs()]
        feed = {("input_ids" if "input_ids" in names else "tokens"): np.array([[0, *ids, 0]], dtype=np.int64),
                "style": style, "speed": np.array([speed], dtype=np.float32)}
        wav, durations = kokoro.sess.run(None, feed)[:2]
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        # Token j (offset by the leading pad) spans cum[j+1] .. cum[j+2] samples.
        cum = np.concatenate([[0], np.cumsum(np.asarray(durations).reshape(-1))]) * FRAME_SAMPLES
        times = []
        for start, end in spans:
            s = cum[start + 1] / SAMPLE_RATE
            e = cum[max(end, start) + 1] / SAMPLE_RATE
            times.append((s, max(s, e)))
        return wav, times

    @staticmethod
    def _trim(wav, times):
        """Trim the model's leading/trailing silence, keeping soft word endings intact."""
        level = np.abs(wav)
        loud = np.nonzero(level > max(0.004, float(level.max()) * 0.01))[0]
        if not len(loud):
            return wav, times
        s = max(0, loud[0] - int(0.04 * SAMPLE_RATE))
        e = min(len(wav), loud[-1] + int(0.15 * SAMPLE_RATE))
        length = (e - s) / SAMPLE_RATE
        shift = s / SAMPLE_RATE
        times = [(min(max(a - shift, 0), length), min(max(b - shift, 0), length)) for a, b in times]
        return wav[s:e], times

    @staticmethod
    def _groups(word_phonemes):
        """Split very long sentences into chunks that fit the model, preferring punctuation."""
        groups, current, size, last_soft = [], [], 0, -1
        for i, pw in enumerate(word_phonemes):
            n = len(pw) + 1
            if current and size + n > MAX_TOKENS:
                cut = last_soft + 1 if last_soft >= 0 else len(current)
                groups.append(current[:cut])
                current = current[cut:]
                size = sum(len(word_phonemes[j]) + 1 for j in current)
                last_soft = -1
            current.append(i)
            size += n
            if pw and pw[-1] in ",;:.!?—…":
                last_soft = len(current) - 1
        if current:
            groups.append(current)
        return groups

    # ---- PyTorch engine (hexgrad/Kokoro-82M via the `kokoro` package) --------------------------
    def _torch_pipeline(self, lang_code):
        if self.torch_dir or _hf_cached():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")      # model is local: never touch the network
        import torch
        from kokoro import KModel, KPipeline
        if self._tmodel is None:
            kw = {"repo_id": "hexgrad/Kokoro-82M"}
            d = Path(self.torch_dir) if self.torch_dir else None
            if d and (d / "kokoro-v1_0.pth").exists() and (d / "config.json").exists():
                kw.update(config=str(d / "config.json"), model=str(d / "kokoro-v1_0.pth"))
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._tmodel = KModel(**kw).to(device).eval()
        if lang_code not in self._pipes:
            self._pipes[lang_code] = KPipeline(lang_code=lang_code, repo_id="hexgrad/Kokoro-82M", model=self._tmodel)
        return self._pipes[lang_code]

    def _synth_torch(self, text, words, voice, speed):
        """Synthesize one sentence with KPipeline and map its token timestamps onto our words."""
        pipe = self._torch_pipeline(voice[0] if voice[0] in LANGUAGES else "a")
        local = Path(self.torch_dir) / "voices" / f"{voice}.pt" if self.torch_dir else None
        voice_arg = str(local) if local and local.exists() else voice
        spans, pos = [], 0                                   # character span of each word
        for w in words:
            start = text.index(w, pos)
            spans.append((start, start + len(w)))
            pos = start + len(w)
        word_at = lambda c: next((i for i, (a, b) in enumerate(spans) if a <= c < b), None)  # noqa: E731
        pieces, starts, ends, offset, cursor = [], {}, {}, 0.0, 0
        for result in pipe(text, voice=voice_arg, speed=float(speed), split_pattern=None):
            if result.audio is None:
                continue
            wav = result.audio.detach().cpu().numpy().astype(np.float32).reshape(-1)
            stamped = []
            for tok in (result.tokens or []):
                t = getattr(tok, "text", "") or ""
                found = text.find(t, cursor) if t else -1
                if found < 0:
                    continue
                cursor = found + len(t)
                wi = word_at(found)
                if wi is None or not re.search(r"\w", t) or tok.start_ts is None or tok.end_ts is None:
                    continue
                stamped.append((wi, float(tok.start_ts), float(tok.end_ts)))
            wav, trimmed = self._trim(wav, [(a, b) for _, a, b in stamped])
            if pieces:
                gap = np.zeros(int(0.08 * SAMPLE_RATE), dtype=np.float32)
                pieces.append(gap)
                offset += len(gap) / SAMPLE_RATE
            for (wi, _, _), (a, b) in zip(stamped, trimmed):
                starts[wi] = min(starts.get(wi, 1e9), a + offset)
                ends[wi] = max(ends.get(wi, 0.0), b + offset)
            pieces.append(wav)
            offset += len(wav) / SAMPLE_RATE
        if not pieces:
            raise RuntimeError("Kokoro returned no audio for: " + text[:60])
        audio = np.concatenate(pieces)
        total = len(audio) / SAMPLE_RATE
        times, last = [], 0.0
        if not starts:   # no timestamps (non-English voices): spread words by length as a fallback
            weights = np.cumsum([0] + [len(w) + 1 for w in words]) / (sum(len(w) + 1 for w in words) or 1)
            return audio, [(total * weights[i], total * weights[i + 1]) for i in range(len(words))]
        for i in range(len(words)):
            a = starts.get(i, last)
            b = max(a, ends.get(i, a))
            times.append((a, b))
            last = b
        return audio, times

    def _synth_onnx(self, words, voice, speed):
        lang = LANGUAGES.get(voice[0], ("", "en-us"))[1]
        word_ph = self._phonemes_per_word(words, lang)
        pieces, times, offset = [], [], 0.0
        for group in self._groups(word_ph):
            wav, group_times = self._run([word_ph[i] for i in group], voice, float(speed))
            wav, group_times = self._trim(wav, group_times)
            if pieces:
                gap = np.zeros(int(0.08 * SAMPLE_RATE), dtype=np.float32)
                pieces.append(gap)
                offset += len(gap) / SAMPLE_RATE
            pieces.append(wav)
            times += [(a + offset, b + offset) for a, b in group_times]
            offset += len(wav) / SAMPLE_RATE
        return np.concatenate(pieces).astype(np.float32), times

    def _model_id(self):
        if self.engine == "torch":
            pth = Path(self.torch_dir or "") / "kokoro-v1_0.pth"
            return f"torch:{pth.stat().st_size if pth.exists() else 'hub'}"
        return Path(self.model_path).stat().st_size      # unchanged so existing ONNX caches stay valid

    def sentence(self, text, voice, speed, emotion=None, context=None):
        """Audio + per-word timings for one sentence, from cache when possible.
        Returns (audio, words, times, was_cached)."""
        words = text.split()
        model = (f"cartesia:{CARTESIA_MODEL}" if is_cartesia(voice) else
                 f"eleven:{ElevenLabs.model}" if is_eleven(voice) else self._model_id())
        emotion = emotion if is_cartesia(voice) else None     # only Cartesia takes an emotion
        context = tuple(context) if is_eleven(voice) and context and any(context) else None
        parts = [CACHE_VERSION, model, voice, round(float(speed), 3), text] + ([emotion] if emotion else []) \
            + ([list(context)] if context else [])
        key = hashlib.sha1(json.dumps(parts).encode()).hexdigest()
        path = self.cache_dir / f"{key}.npz"
        if path.exists():
            try:
                data = np.load(path)
                return data["audio"], words, [tuple(t) for t in data["times"].tolist()], True
            except Exception:
                path.unlink(missing_ok=True)
        if is_cartesia(voice) or is_eleven(voice):
            cloud, name = (self.cartesia, "Cartesia") if is_cartesia(voice) else (self.eleven, "ElevenLabs")
            if not cloud:
                raise CartesiaError(f"Add your {name} API key in Settings → Voice to use {name} voices.")
            with self._lock:      # one paid request per sentence, even if two tasks ask at the same time
                if path.exists():
                    data = np.load(path)
                    return data["audio"], words, [tuple(t) for t in data["times"].tolist()], True
                if cloud is self.cartesia:
                    audio, api_words, starts, ends = cloud.synth(text, voice, speed, emotion)
                    times = _align_words(words, api_words, starts, ends, len(audio) / SAMPLE_RATE)
                else:
                    audio, times = cloud.synth_words(text, words, voice, speed, context)
                audio, times = self._trim(audio, times)
                self._store(path, audio, times)
            return audio, words, times, False
        else:
            with self._lock:
                if self.engine == "torch":
                    audio, times = self._synth_torch(text, words, voice, speed)
                else:
                    audio, times = self._synth_onnx(words, voice, speed)
        audio = self._store(path, audio, times)
        return audio, words, times, False

    @staticmethod
    def _store(path, audio, times):
        audio = audio.astype(np.float32)
        tmp = path.with_suffix(".tmp.npz")
        np.savez(tmp, audio=audio, times=np.array(times, dtype=np.float64))
        tmp.replace(path)
        return audio

    def scene(self, script, voice, speed, sentence_gap=0.2, words_per_caption=5, cancelled=None, style="story"):
        """Narration for one scene. Returns (audio, captions, sentences_synthesized)."""
        parts, captions, t, fresh = [], [], 0.0, 0
        poem = parse_style(style)
        if poem:      # (line, pause before it): longer after a verse, never shorter than Sentence pause
            line_gap, verse_gap = max(sentence_gap, poem["line"]), max(sentence_gap, poem["verse"])
            units = [(line, verse_gap if j == 0 else line_gap)
                     for verse in re.split(r"\n\s*\n", script.strip())
                     for j, line in enumerate(split_sentences(verse, lines=True))]
            speed, emotion = float(speed) * poem["speed"], poem["emotion"]
        else:
            units, emotion = [(s, sentence_gap) for s in split_sentences(script)], None
        for i, (sentence, pause) in enumerate(units):
            if cancelled and cancelled():
                raise Cancelled()
            context = (units[i - 1][0] if i else "", units[i + 1][0] if i + 1 < len(units) else "")
            audio, words, times, cached = self.sentence(sentence, voice, speed, emotion, context)
            fresh += 0 if cached else 1
            if i:
                gap = np.zeros(int(pause * SAMPLE_RATE), dtype=np.float32)
                parts.append(gap)
                t += len(gap) / SAMPLE_RATE
            captions += build_captions(words, times, words_per_caption, offset=t)
            parts.append(audio)
            t += len(audio) / SAMPLE_RATE
        audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return audio, captions, fresh


def _hf_cached():
    from .config import hf_cache_dirs
    return bool(hf_cache_dirs())
