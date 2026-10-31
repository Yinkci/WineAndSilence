@echo off
rem StoryCraft video maker - double-click to start. Close this window to stop.
rem Optional: put the full path of a python.exe in python_path.txt to choose which Python is used.
cd /d "%~dp0"
title StoryCraft

set "PYEXE="
if exist "python_path.txt" set /p PYEXE=<"python_path.txt"
if defined PYEXE if not exist "%PYEXE%" set "PYEXE="
if not defined PYEXE (
  where py >nul 2>nul && set "PYEXE=py"
)
if not defined PYEXE (
  for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYEXE set "PYEXE=%%P"
)
if not defined PYEXE goto :nopython

"%PYEXE%" "%~dp0tools\launch.py"
if errorlevel 1 goto :fail
exit /b 0

:nopython
echo Python 3.10 or newer was not found on this PC.
echo Install it from https://www.python.org/downloads/ and start StoryCraft again.
pause
exit /b 1

:fail
echo.
echo Something went wrong starting StoryCraft. See the message above.
pause
