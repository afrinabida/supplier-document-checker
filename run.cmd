@echo off
setlocal
set "PYTHONUTF8=1"
set "PROJECT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PROJECT_PYTHON%" (
    echo Project environment missing. Run: python -m venv .venv
    exit /b 1
)
"%PROJECT_PYTHON%" -c "from langgraph.checkpoint.sqlite import SqliteSaver; import streamlit"
if errorlevel 1 (
    echo Install dependencies with: "%PROJECT_PYTHON%" -m pip install -r "%~dp0requirements.txt"
    exit /b 1
)
"%PROJECT_PYTHON%" "%~dp0launch_local.py" %*
exit /b %errorlevel%
