@echo off
rem Creates dist\StoryCraft.zip with only the app (no personal images, projects, videos or caches).
cd /d "%~dp0"
set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE (
  where py >nul 2>nul && set "PYEXE=py"
)
if not defined PYEXE set "PYEXE=python"
"%PYEXE%" "%~dp0tools\make_share_zip.py"
pause
