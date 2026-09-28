@echo off
rem Run KnoxMap from this checkout. Skips the release packager so code changes
rem can be tried immediately. Needs 64-bit Python 3.10+ the first time.
rem
rem The console stays open so errors are visible. Close the KnoxMap window,
rem or press Ctrl+C here, to stop it.
rem
rem This launch is debug mode: a second window profiles Python function
rem calls and writes KnoxMap\logs\debug-log.log. A normal launch does not.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

rem python3 is the normal interpreter. python on this PC is free-threaded,
rem and SciPy never finishes a room layout there.
where python3 >nul 2>nul || (
  echo Need the python3 command. python is free-threaded and SciPy does not run on it.
  pause
  exit /b 1
)
findstr /i /c:"python3.14t" /c:"freethreaded" "KnoxMap\.venv\pyvenv.cfg" >nul 2>nul && (
  echo The Python environment is free-threaded. Recreating it with python3...
  rmdir /s /q "KnoxMap\.venv"
)
if not exist "KnoxMap\.venv\Scripts\python.exe" (
  echo Creating the Python environment with python3...
  python3 -m venv KnoxMap\.venv || (
    pause
    exit /b 1
  )
  echo Installing Python packages...
  "KnoxMap\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r KnoxMap\requirements.txt || (
    pause
    exit /b 1
  )
)

cd /d "%~dp0KnoxMap"
set KNOXMAP_DEBUG=1
echo Profiler window on. Writing %~dp0KnoxMap\logs\debug-log.log
".venv\Scripts\python.exe" knoxmap.py %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo KnoxMap exited with code %ERR%.
  pause
)
exit /b %ERR%
