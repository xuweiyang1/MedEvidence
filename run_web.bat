@echo off
rem Portable Windows launcher for the MedEvidence web UI.
rem Uses web_app\.python_path.txt if present, otherwise python from PATH.
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "USE_TF=0"
set "USE_FLAX=0"
set "USE_TORCH=1"

set "PYTHON="
if exist "web_app\.python_path.txt" set /p PYTHON=<"web_app\.python_path.txt"
if not defined PYTHON set "PYTHON=python"

if not exist "web_app\app.py" (
    echo [ERROR] web_app\app.py not found. Run this file from the repository root.
    pause
    exit /b 1
)

echo Starting MedEvidence - http://127.0.0.1:8501
"%PYTHON%" -m streamlit run "web_app\app.py" --server.address 127.0.0.1 --server.port 8501

if errorlevel 1 (
    echo.
    echo [ERROR] Streamlit exited with an error. Install dependencies first:
    echo   python -m pip install -r requirements.txt -r requirements-web.txt
    pause
)

endlocal
