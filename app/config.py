"""Locate FFmpeg and a local Kokoro installation; remember choices in config.json.

Two kinds of Kokoro are supported:
- ONNX:    the kokoro-onnx package + kokoro-v1.0.onnx / voices-v1.0.bin
- PyTorch: the `kokoro` package (hexgrad/Kokoro-82M) + kokoro-v1_0.pth / config.json / voices/*.pt
config.json is per machine: paths saved there are only used if they exist on this PC.
"""
import glob
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
MY_DEFAULTS_FILE = ROOT / "my_defaults.json"   # your own starting settings for new projects (this PC)
MEDIA_DIR = ROOT / "media"            # copies of uploaded images (originals are never touched)
WORKSPACE = ROOT / "workspace"        # caches, temporary job folders, previews
PROJECTS_DIR = ROOT / "projects"
EXPORTS_DIR = ROOT / "exports"
ARTSTYLE_DIR = ROOT / "artstyle"      # images here = the art style for generated scene images
LUTS_DIR = ROOT / "luts"              # built-in color grades: every .cube here is a Look (this PC only)

for d in (MEDIA_DIR, WORKSPACE, PROJECTS_DIR, EXPORTS_DIR, ARTSTYLE_DIR, LUTS_DIR):
    d.mkdir(exist_ok=True)

HOME = Path.home()
# Folders searched (a few levels deep) for Kokoro model files.
SEARCH_ROOTS = [ROOT / "models", HOME, HOME / "Documents", HOME / "Downloads", HOME / "Desktop",
                Path("C:/"), Path("C:/Nyx/voices")]
PATH_KEYS = ("ffmpeg", "ffprobe", "kokoro_model", "kokoro_voices", "kokoro_torch_dir")


def _find_exe(name):
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        # winget installs FFmpeg here; the PATH update only applies to new shells.
        os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin", name + ".exe"),
        os.path.join(local, "Microsoft", "WinGet", "Links", name + ".exe"),
        r"C:\ffmpeg\bin\%s.exe" % name, r"C:\ffmpeg\*\bin\%s.exe" % name,
        r"C:\Program Files\ffmpeg\bin\%s.exe" % name, r"C:\Program Files\ffmpeg\*\bin\%s.exe" % name,
        r"C:\ProgramData\chocolatey\bin\%s.exe" % name,
        str(HOME / "scoop" / "shims" / (name + ".exe")),
        str(ROOT / "ffmpeg" / "bin" / (name + ".exe")),
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return ""


def _walk_for(filename, max_depth=3):
    """Look for a file in the usual folders without crawling the whole disk."""
    skip = {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
            "appdata", "node_modules", ".git", "site-packages", "__pycache__"}
    for root in SEARCH_ROOTS:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if filename in filenames:
                return Path(dirpath) / filename
            if len(Path(dirpath).parts) - base_depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
    return None


def find_onnx_kokoro():
    for folder in (Path(r"C:\Nyx\voices\kokoro"), ROOT / "models" / "kokoro"):
        if (folder / "kokoro-v1.0.onnx").exists() and (folder / "voices-v1.0.bin").exists():
            return str(folder / "kokoro-v1.0.onnx"), str(folder / "voices-v1.0.bin")
    model = _walk_for("kokoro-v1.0.onnx")
    if model and (model.parent / "voices-v1.0.bin").exists():
        return str(model), str(model.parent / "voices-v1.0.bin")
    return "", ""


def hf_cache_dirs():
    """Hugging Face cache snapshot folders of hexgrad/Kokoro-82M (where KPipeline downloads to)."""
    hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or HOME / ".cache" / "huggingface") / "hub")
    snaps = sorted((hub / "models--hexgrad--Kokoro-82M" / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [s for s in snaps if (s / "kokoro-v1_0.pth").exists()]


def find_torch_kokoro():
    """A folder with kokoro-v1_0.pth + config.json (a downloaded/cloned Kokoro-82M, or the HF cache)."""
    for snap in hf_cache_dirs():
        return str(snap)
    for folder in (ROOT / "models" / "Kokoro-82M", HOME / "Kokoro-82M", HOME / "Documents" / "Kokoro-82M",
                   HOME / "Downloads" / "Kokoro-82M", HOME / "Desktop" / "Kokoro-82M", Path("C:/Kokoro-82M")):
        if (folder / "kokoro-v1_0.pth").exists():
            return str(folder)
    found = _walk_for("kokoro-v1_0.pth")
    return str(found.parent) if found else ""


def _usable(key, value):
    if key not in PATH_KEYS:
        return bool(value) or value == 0
    return bool(value) and Path(value).exists()


def load():
    saved = {}
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
    cfg = {"ffmpeg": "", "ffprobe": "", "kokoro_engine": "auto", "kokoro_model": "", "kokoro_voices": "",
           "kokoro_torch_dir": "", "port": 8766}     # 8765 belongs to Mr Sidequests
    # Saved values win, but only if they exist on THIS machine (config.json may come from another PC).
    cfg.update({k: v for k, v in saved.items() if _usable(k, v)})
    if not cfg["ffmpeg"]:
        cfg["ffmpeg"] = _find_exe("ffmpeg")
    if not cfg["ffprobe"]:
        cfg["ffprobe"] = (str(Path(cfg["ffmpeg"]).with_name("ffprobe.exe"))
                          if cfg["ffmpeg"] and Path(cfg["ffmpeg"]).with_name("ffprobe.exe").exists()
                          else _find_exe("ffprobe"))
    engine = cfg.get("kokoro_engine", "auto")
    have_onnx = bool(cfg["kokoro_model"] and cfg["kokoro_voices"])
    if not have_onnx and not cfg["kokoro_torch_dir"]:      # search only when nothing is known yet
        if engine in ("auto", "onnx"):
            cfg["kokoro_model"], cfg["kokoro_voices"] = find_onnx_kokoro()
        if engine in ("auto", "torch") and not cfg["kokoro_model"]:
            cfg["kokoro_torch_dir"] = find_torch_kokoro()
    save(cfg)
    return cfg


def save(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


CONFIG = load()
