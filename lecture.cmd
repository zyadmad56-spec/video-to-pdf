@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
  set "PYTHON=%ROOT%.venv\Scripts\python.exe"
) else if exist "%ROOT%..\.venv\Scripts\python.exe" (
  set "PYTHON=%ROOT%..\.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)
"%PYTHON%" "%ROOT%scripts\lecture.py" %*
exit /b %ERRORLEVEL%
