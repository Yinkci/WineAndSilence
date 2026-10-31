"""Build dist/StoryCraft.zip: the app only, ready to give to someone else.

Left out on purpose: .venv (PC-specific), config.json (this PC's paths), media/projects/exports
(your images, projects and videos), workspace (caches), .git and dist itself.
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["app", "tools", "sample", "start.bat", "make_share_zip.bat", "README.md", "requirements.txt"]
SKIP_PARTS = {"__pycache__"}


def main():
    out = ROOT / "dist" / "StoryCraft.zip"
    out.parent.mkdir(exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            src = ROOT / name
            files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
            for f in files:
                rel = f.relative_to(ROOT)
                if SKIP_PARTS & set(rel.parts) or f.suffix == ".pyc":
                    continue
                z.write(f, Path("StoryCraft") / rel)
                count += 1
        for empty in ("media", "projects", "exports"):
            z.writestr(f"StoryCraft/{empty}/", "")
    print(f"Created {out} ({count} files, {out.stat().st_size / 1024:.0f} KB)")
    print("Give this ZIP to the other person: they unzip it anywhere and double-click start.bat.")


if __name__ == "__main__":
    main()
