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

rem Room layout, terrain, way picking, and clipping share one Rust library, helper.dll.
rem The map compiler is knox-compile. Both are required.
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
call :rust_helper
if errorlevel 1 exit /b 1
call :rust_compile
if errorlevel 1 exit /b 1

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

:rust_helper
where cargo >nul 2>nul || (
  echo Rust is not installed. KnoxMap needs it for the helper library and knox-compile.
  echo Install it from https://rustup.rs
  pause
  exit /b 1
)
powershell -NoProfile -Command "$dll='Helpers\target\release\helper.dll'; $src=@(Get-ChildItem -Path 'Helpers' -Recurse -File -Include *.rs,Cargo.toml -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '\\target\\' }); if(-not $src){exit 0}; $new=($src | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime; if((Test-Path -LiteralPath $dll) -and ((Get-Item -LiteralPath $dll).LastWriteTime -ge $new)){exit 0}; exit 2"
if errorlevel 2 (
  echo Building helper...
  pushd Helpers
  cargo build --release -p helper
  if errorlevel 1 (
    popd
    echo helper did not build.
    pause
    exit /b 1
  )
  popd
)
exit /b 0

:rust_compile
where cargo >nul 2>nul || exit /b 1
powershell -NoProfile -Command "$exe='compiler\target\release\knox-compile.exe'; $src=@(Get-ChildItem -Path 'compiler' -Recurse -File -Include *.rs,Cargo.toml,Cargo.lock -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '\\target\\' }); if(-not $src){exit 0}; $new=($src | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime; if((Test-Path -LiteralPath $exe) -and ((Get-Item -LiteralPath $exe).LastWriteTime -ge $new)){exit 0}; exit 2"
if errorlevel 2 (
  echo Building the map compiler...
  cargo build --release --manifest-path compiler\Cargo.toml
  if errorlevel 1 (
    echo The map compiler did not build.
    pause
    exit /b 1
  )
)
exit /b 0
