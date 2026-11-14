@echo off
rem Windows: double-click this file to start Model Downloader.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: setting things up, about a minute...
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python.exe -c "import huggingface_hub" >nul 2>&1 || .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
.venv\Scripts\python.exe app.py
pause
