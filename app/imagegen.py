"""Scene images with ChatGPT, through the official Codex CLI signed in with your ChatGPT account.

No API key is used: Codex's built-in image generation counts against your ChatGPT plan's Codex
limits. (Same approach as Arkon.) Each image is one isolated, non-interactive `codex exec` run:
read-only sandbox, no user config/rules/session files, shell/patching/apps/web search off, only
`features.image_generation` on, and an environment without API keys.

`codex exec --json` reports the thread id but not the image, so the image is read from the file
Codex saves at <CODEX_HOME>/generated_images/<thread id>/: only that thread's folder, only files
written during this run.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import CONFIG, MEDIA_DIR

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IMAGE_TIMEOUT = 420
ENV_ALLOW = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE", "HOMEDRIVE",
             "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "USERNAME", "USERDOMAIN",
             "COMPUTERNAME", "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG",
             "LC_ALL", "TZ", "CODEX_HOME"}
THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LIMIT = re.compile(r"usage.?limit|rate.?limit|quota", re.I)


class ImageError(Exception):
    pass


class Cancelled(Exception):
    pass


def find_codex():
    """The native codex.exe: config.json 'codex_command', PATH, the npm package, or the Codex desktop app."""
    configured = CONFIG.get("codex_command")
    if configured and Path(configured).is_file():
        return _native(configured)
    for d in os.environ.get("PATH", "").split(os.pathsep) + [str(Path.home() / ".local" / "bin")]:
        for name in ("codex.exe", "codex.cmd"):
            candidate = Path(d) / name
            if candidate.is_file():
                native = _native(str(candidate))
                if native:
                    return native
    bundled = glob.glob(str(Path.home() / "AppData/Local/OpenAI/Codex/bin/*/codex.exe"))
    bundled.sort(key=os.path.getmtime, reverse=True)
    return bundled[0] if bundled else None


def _native(path):
    if path.lower().endswith((".cmd", ".ps1")):
        # Never run shell wrappers; use the npm package's native binary.
        bins = glob.glob(str(Path(path).parent / "node_modules/@openai/codex*/vendor/*/codex/codex.exe"))
        return bins[0] if bins else None
    return path


def _env():
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}   # no API keys reach Codex
    return env


def _run(exe, args, stdin="", timeout=30):
    p = subprocess.run([exe, *args], input=stdin, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=_env(), creationflags=NO_WINDOW)
    return p.returncode, p.stdout, p.stderr


_status_cache = {"at": 0.0, "value": None}


def status(force=False):
    """Is image generation available? No model request is made (free to call)."""
    if not force and _status_cache["value"] and time.time() - _status_cache["at"] < 60:
        return _status_cache["value"]
    exe = find_codex()
    result = {"ready": False, "version": None, "message": "", "codex": exe}
    try:
        if not exe:
            result["message"] = ("Codex was not found. Install the ChatGPT Codex app (or `npm i -g @openai/codex`), "
                                 "then sign in with your ChatGPT account.")
            return result
        code, out, _ = _run(exe, ["--version"])
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if code != 0 or not m:
            result["message"] = "Codex could not report its version."
            return result
        result["version"] = m.group(1)
        _, help_out, _ = _run(exe, ["exec", "--help"])
        if "--ignore-user-config" not in help_out or "--ephemeral" not in help_out:
            result["message"] = "Update Codex: this version does not support isolated runs."
            return result
        code, out, err = _run(exe, ["login", "status"])
        if code != 0:
            result["message"] = "Codex is not signed in. Open a terminal and run:  codex login  (choose Sign in with ChatGPT)."
            return result
        if "chatgpt" not in (out + " " + err).lower():
            result["message"] = "Codex is signed in with an API key. Run  codex logout  then  codex login  and choose ChatGPT."
            return result
        _, feats, _ = _run(exe, ["features", "list", "-c", "features.image_generation=true"])
        fm = re.search(r"^image_generation\s+(.+?)\s+(true|false)\s*$", feats, re.M)
        if not fm or fm.group(1).strip() == "removed" or fm.group(2) != "true":
            result["message"] = "This Codex version doesn't offer image generation. Update Codex, then check again."
            return result
        result.update(ready=True, message=f"Codex {result['version']} · signed in with ChatGPT · image generation available")
        return result
    except (OSError, subprocess.SubprocessError) as e:
        result["message"] = f"Codex could not be started: {e}"
        return result
    finally:
        _status_cache.update(at=time.time(), value=result)


_models_cache = {"at": 0.0, "value": None}


def models(force=False):
    """The models this Codex offers (from `codex debug models`; no model request, no usage).
    Returns [{"slug", "name"}] best first; the first one is what Codex uses by default."""
    if not force and _models_cache["value"] is not None and time.time() - _models_cache["at"] < 600:
        return _models_cache["value"]
    exe = find_codex()
    result = []
    if exe:
        for extra in ([], ["--bundled"]):                   # live catalog first, built-in list as fallback
            try:
                code, out, _ = _run(exe, ["debug", "models", *extra], timeout=40)
                data = json.loads(out)
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
            items = data.get("models", data) if isinstance(data, dict) else data
            listed = [m for m in items if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"]
            listed.sort(key=lambda m: m.get("priority", 999))
            result = [{"slug": m["slug"], "name": m.get("display_name") or m["slug"]} for m in listed]
            if result:
                break
    _models_cache.update(at=time.time(), value=result)
    return result


def chosen_model():
    """The model picked in Settings, if it's still offered by this Codex; None = Codex default."""
    slug = CONFIG.get("image_model") or ""
    return slug if slug and any(m["slug"] == slug for m in models()) else None


def isolated_args(schema, output, references):
    args = ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "--json", "--color", "never", "--output-schema", schema,
            "--output-last-message", output,
            "-c", 'approval_policy="never"', "-c", "features.shell_tool=false", "-c", "features.unified_exec=false",
            "-c", "features.apply_patch_freeform=false", "-c", "features.shell_snapshot=false",
            "-c", "features.apps=false", "-c", "features.image_generation=true",
            "-c", "features.unbounded_connection_retries=false", "-c", 'web_search="disabled"',
            "-c", "project_doc_max_bytes=0"]
    for ref in references:
        args += ["--image", ref]
    model = chosen_model()
    if model:
        args += ["-m", model]
    return args + ["-"]


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _saved_image(thread, started):
    if not thread or not THREAD_ID.match(thread):
        return None
    folder = _codex_home() / "generated_images" / thread
    if not folder.is_dir() or folder.is_symlink():
        return None
    files = [f for f in folder.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
             and f.is_file() and not f.is_symlink() and f.stat().st_mtime >= started - 5]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def generate(prompt, references=(), cancelled=None, on_progress=None):
    """Generate one image. Returns the path of a copy in StoryCraft's media folder."""
    st = status()
    if not st["ready"]:
        raise ImageError(st["message"])
    exe = st["codex"]
    # 941x1672 is the exact 9:16 size Codex's image tool produces (1024x1536 would be 2:3, not 9:16).
    size = "941x1672 (9:16 portrait)"
    # The image tool rejects requests with more than five reference images.
    refs = [str(Path(r)) for r in references if r and Path(r).is_file()][:5]
    work = Path(tempfile.mkdtemp(prefix="storycraft-img-"))
    try:
        schema, output = work / "schema.json", work / "result.json"
        schema.write_text(json.dumps({"type": "object", "additionalProperties": False, "required": ["generated", "problem"],
                                      "properties": {"generated": {"type": "boolean"}, "problem": {"type": ["string", "null"]}}}))
        ref_note = (" Images are attached as references; the request below explains what each one shows, in order."
                    if refs else "")
        instructions = (f"Use your image generation tool exactly once to create the image described below at {size}."
                        f"{ref_note} Do not run commands, read or write files, or browse. If the request cannot be "
                        "generated (for example a safety refusal), do not generate anything else instead. Then reply "
                        'only with JSON: {"generated": true, "problem": null} after generating, or '
                        '{"generated": false, "problem": "<short reason>"}.')
        started = time.time()
        proc = subprocess.Popen([exe, *isolated_args(str(schema), str(output), refs)], cwd=str(work),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=_env(), creationflags=NO_WINDOW)
        proc.stdin.write(instructions + "\n\n" + prompt)
        proc.stdin.close()
        thread, limit, errors, failure = None, False, [], ""
        reader = threading.Thread(target=lambda: errors.extend(proc.stderr.readlines()), daemon=True)
        reader.start()
        watchdog = threading.Timer(IMAGE_TIMEOUT, proc.kill)
        watchdog.start()

        def stop_if_cancelled():
            while proc.poll() is None:
                if cancelled and cancelled():
                    proc.kill()
                    return
                time.sleep(0.3)
        threading.Thread(target=stop_if_cancelled, daemon=True).start()
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type", "")
                if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                    thread = event["thread_id"]
                    if on_progress:
                        on_progress("Generating image with ChatGPT…")
                elif kind in ("turn.failed", "error"):
                    limit = limit or bool(LIMIT.search(json.dumps(event)))
                    # Codex reports the real reason here (stderr only says "Reading prompt from stdin...").
                    err = event.get("error")
                    msg = err.get("message") if isinstance(err, dict) else event.get("message")
                    if isinstance(msg, str) and msg.strip():
                        failure = msg.strip()
            proc.wait()
        finally:
            watchdog.cancel()
        reader.join(timeout=5)
        if cancelled and cancelled():
            raise Cancelled()
        if time.time() - started >= IMAGE_TIMEOUT - 1:
            raise ImageError("Image generation took too long and was stopped. Try again.")
        text = "".join(errors) + "\n" + failure
        if proc.returncode != 0:
            if limit or LIMIT.search(text):
                raise ImageError("Your ChatGPT image limit for Codex has been reached. Try again after it resets.")
            if re.search(r"not logged in|unauthoriz|authentication|401", text, re.I):
                raise ImageError("Codex needs sign-in. Run  codex login  and choose ChatGPT.")
            reason = failure or next((l.strip() for l in reversed(errors) if l.strip() and "reading prompt from stdin" not in l.lower()), "")
            raise ImageError("Codex could not generate the image. " + (reason[:400] or "No reason was given; try again."))
        try:
            reply = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reply = {}
        if reply.get("generated") is False:
            raise ImageError("ChatGPT did not generate the image" + (f": {reply['problem'][:300]}" if reply.get("problem") else "."))
        saved = _saved_image(thread, started)
        if not saved:
            raise ImageError("Codex finished but no image file was found for this run. Update Codex and try again.")
        if saved.stat().st_size > 40 * 1024 * 1024:
            raise ImageError("The generated image file is too large.")
        data = saved.read_bytes()
        target = MEDIA_DIR / f"gen_{hashlib.sha1(data).hexdigest()[:16]}{saved.suffix.lower()}"
        if not target.exists():
            target.write_bytes(data)
        return str(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
