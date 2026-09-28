@echo off
rem Install what this checkout needs before debug_run.bat or build_releases.bat:
rem   KnoxMap\.venv          Python packages from KnoxMap\requirements.txt
rem   desktop\node_modules   Electron window
rem
rem Needs 64-bit Python 3.10+ and Node.js 22+ already installed.
rem Packing Linux and Mac still needs Docker. The map compiler is downloaded
rem the first time KnoxMap opens.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

set MISSING=0
rem python3 is the normal interpreter. python on this PC is free-threaded,
rem and SciPy never finishes a room layout there.
where python3 >nul 2>nul || (
  echo Need the python3 command. python is free-threaded and SciPy does not run on it.
  set MISSING=1
)
findstr /i /c:"python3.14t" /c:"freethreaded" "KnoxMap\.venv\pyvenv.cfg" >nul 2>nul && (
  echo The Python environment is free-threaded. Recreating it with python3...
  rmdir /s /q "KnoxMap\.venv"
)
where node >nul 2>nul || (
  echo Node.js is not installed. Install Node 22 or newer, then run this again.
  set MISSING=1
)
if not "%MISSING%"=="0" (
  pause
  exit /b 1
)

if not exist "KnoxMap\.venv\Scripts\python.exe" (
  echo Creating the Python environment with python3...
  python3 -m venv KnoxMap\.venv || (
    pause
    exit /b 1
  )
)

echo Installing Python packages...
"KnoxMap\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r KnoxMap\requirements.txt pyinstaller || (
  pause
  exit /b 1
)

echo Installing window packages...
pushd desktop
call npm install
set ERR=%ERRORLEVEL%
popd
if not "%ERR%"=="0" (
  pause
  exit /b %ERR%
)

echo.
echo Requirements are installed. Run debug_run.bat to start KnoxMap.
pause
endlocal
