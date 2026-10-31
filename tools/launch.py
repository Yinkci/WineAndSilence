"""StoryCraft launcher (standard library only, so it runs before anything is installed).

1. Finds the Pythons on this PC and picks one that already has Kokoro
   (the `kokoro` + `torch` packages for hexgrad/Kokoro-82M, or `kokoro-onnx`).
   A path written in python_path.txt (next to start.bat) always wins.
2. Prepares the app environment: a .venv that shares that Python's packages (rebuilt
   automatically if it was created on another PC), or that Python itself if it is
   already a virtual/conda environment.
3. Installs only the few small packages that are missing, then starts the app.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
NEEDED = {"fastapi": "fastapi", "uvicorn": "uvicorn", "multipart": "python-multipart",
          "soundfile": "soundfile", "numpy": "numpy"}          # import name -> pip name
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROBE = (
    "import sys, os, json, importlib.util as u\n"
    "def f(m):\n"
    "    try: return u.find_spec(m) is not None\n"
    "    except Exception: return False\n"
    "print(json.dumps({'v': list(sys.version_info[:2]), 'exe': sys.executable,"
    " 'onnx': f('kokoro_onnx'), 'torch': f('kokoro') and f('torch'), 'tk': f('tkinter'),"
    " 'env': sys.prefix != sys.base_prefix or os.path.isdir(os.path.join(sys.prefix, 'conda-meta'))}))"
)


def say(msg=""):
    print(msg, flush=True)


def candidates():
    found = []
    pinned = ROOT / "python_path.txt"
    if pinned.exists():
        line = pinned.read_text(encoding="utf-8").strip().splitlines()
        if line and line[0].strip():
            found.append(("python_path.txt", line[0].strip().strip('"')))
    if os.environ.get("STORYCRAFT_PYTHON"):
        found.append(("STORYCRAFT_PYTHON", os.environ["STORYCRAFT_PYTHON"]))
    found.append(("launcher", sys.executable))
    for cmd in (["py", "-0p"], ["where", "python"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            m = re.search(r"[A-Za-z]:\\.*?\.exe", line)      # paths may contain spaces
            if m and "windowsapps" not in m.group(0).lower():
                found.append((cmd[0], m.group(0)))
    unique, seen = [], set()
    for source, exe in found:
        key = os.path.normcase(os.path.abspath(exe))
        if key not in seen and Path(exe).exists():
            seen.add(key)
            unique.append((source, exe))
    return unique


def probe(exe):
    try:
        out = subprocess.run([exe, "-c", PROBE], capture_output=True, text=True, timeout=120,
                             creationflags=NO_WINDOW)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def choose():
    infos = []
    for source, exe in candidates():
        info = probe(exe)
        if info and tuple(info["v"]) >= (3, 10):
            info["source"] = source
            infos.append(info)
    if not infos:
        return None
    pinned = [i for i in infos if i["source"] in ("python_path.txt", "STORYCRAFT_PYTHON")]
    if pinned:
        return pinned[0]
    return max(infos, key=lambda i: (i["onnx"] or i["torch"], i["torch"] and not i["onnx"], tuple(i["v"])))


def venv_matches(base_exe):
    cfg = VENV / "pyvenv.cfg"
    if not (VENV / "Scripts" / "python.exe").exists() or not cfg.exists():
        return False
    home = ""
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lower().startswith("home"):
            home = line.split("=", 1)[1].strip()
    return os.path.normcase(os.path.abspath(home)) == os.path.normcase(os.path.dirname(os.path.abspath(base_exe)))


def missing_modules(python):
    code = ("import importlib.util as u, json; print(json.dumps([m for m in %r if u.find_spec(m) is None]))"
            % list(NEEDED))
    out = subprocess.run([python, "-c", code], capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return list(NEEDED)


def main():
    say("Starting StoryCraft…")
    info = choose()
    if not info:
        say("Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/")
        return 1
    engine = "Kokoro (ONNX)" if info["onnx"] else "Kokoro (PyTorch / Kokoro-82M)" if info["torch"] else None
    say(f"Python {info['v'][0]}.{info['v'][1]}: {info['exe']}")
    if engine:
        say(f"Found {engine} in this Python.")
    else:
        say("WARNING: none of the Pythons on this PC has Kokoro installed (the `kokoro` + `torch` packages,")
        say("or kokoro-onnx). If Kokoro lives in a separate environment, put the path of its python.exe")
        say(f"into {ROOT / 'python_path.txt'} and start again.")
    if not info["tk"]:
        say("Note: this Python has no tkinter, so the Save/Open/Browse windows won't appear.")

    if info["env"]:
        python = info["exe"]            # already a venv/conda env: use it directly
    else:
        if not venv_matches(info["exe"]):
            if VENV.exists():
                say("Rebuilding the app environment for this PC…")
                shutil.rmtree(VENV, ignore_errors=True)
            else:
                say("First start: creating the app environment (reuses your installed packages)…")
            if subprocess.call([info["exe"], "-m", "venv", "--system-site-packages", str(VENV)]) != 0:
                say("Could not create the app environment (.venv).")
                return 1
        python = str(VENV / "Scripts" / "python.exe")

    missing = missing_modules(python)
    if missing:
        packages = [NEEDED[m] for m in missing]
        say("Installing: " + ", ".join(packages) + " (one time, needs internet)…")
        if subprocess.call([python, "-m", "pip", "install", "--disable-pip-version-check", *packages]) != 0:
            say("Installing packages failed. Check the internet connection and try again.")
            return 1

    os.chdir(ROOT)
    return subprocess.call([python, "-m", "app.main"])


if __name__ == "__main__":
    sys.exit(main())
