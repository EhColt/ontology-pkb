@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" kb.py update %*
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 kb.py update %*
  ) else (
    python kb.py update %*
  )
)
set "KB_EXIT=%ERRORLEVEL%"
echo.
if not "%KB_EXIT%"=="0" echo Update failed. Read the error above; rerun to resume.
pause
exit /b %KB_EXIT%
