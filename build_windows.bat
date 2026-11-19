@echo off
setlocal
cd /d "%~dp0"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.12 x64 from python.org and include the Python Launcher.
  pause
  exit /b 1
)
py -3.12 -m venv .venv-build
if errorlevel 1 goto failed
.venv-build\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv-build\Scripts\python.exe -m pip install -r requirements-dev.txt -r requirements-build.txt
if errorlevel 1 goto failed
.venv-build\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed
.venv-build\Scripts\python.exe build_windows.py
if errorlevel 1 goto failed
echo Build completed. The portable ZIP is in the release folder.
pause
exit /b 0
:failed
echo Build failed. Please keep this window's error output.
pause
exit /b 1
