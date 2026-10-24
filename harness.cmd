@echo off
rem Runs the harness with the project's virtual environment, whatever Python is on PATH.
rem Usage (PowerShell or cmd):  .\harness doctor   |   .\harness run "task"   |   .\harness daemon
setlocal
set "VENV_PY=%HARNESS_VENV%\Scripts\python.exe"
if not defined HARNESS_VENV set "VENV_PY=%USERPROFILE%\.venvs\agenticps\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo Virtual environment not found at "%VENV_PY%". See README "Setup".
  exit /b 2
)
set PYTHONIOENCODING=utf-8
"%VENV_PY%" -m harness %*
