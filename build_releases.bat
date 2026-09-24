@echo off
rem Build the player downloads into release\ (gitignored):
rem   KnoxMap-v…-windows.exe       window
rem   KnoxMap-v…-windows-cli.exe   command line, no window
rem   KnoxMap-v…-linux.AppImage
rem   KnoxMap-v…-linux-cli
rem   KnoxMap-v…-macos.zip     (KnoxMap.app, plus knoxmap-cli beside it)
rem
rem Windows is packed on this PC. Linux and Mac are packed in Docker.
rem Needs 64-bit Python 3.10+, Node.js, and Docker Desktop.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

where node >nul 2>nul || (
  echo Node.js is not installed. Install Node 22 or newer, then run this again.
  exit /b 1
)
where docker >nul 2>nul || (
  echo Docker is not installed. Install Docker Desktop, then run this again.
  echo Linux and Mac cannot be packed by Windows itself.
  exit /b 1
)

set PY=
if exist "KnoxMap\.venv\Scripts\python.exe" set PY=KnoxMap\.venv\Scripts\python.exe
if not defined PY where py >nul 2>nul && py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) and sys.maxsize > 2**32 else 1)" >nul 2>nul && set PY=py -3
if not defined PY python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) and sys.maxsize > 2**32 else 1)" >nul 2>nul && set PY=python
if not defined PY (
  echo Need 64-bit Python 3.10 or newer. Run KnoxMap\Setup.bat, or install Python, then run this again.
  exit /b 1
)

if not exist "KnoxMap\.venv\Scripts\python.exe" (
  echo Creating the Python environment...
  %PY% -m venv KnoxMap\.venv || exit /b 1
)

if not exist "release" mkdir release

echo Installing Python packages...
"KnoxMap\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r KnoxMap\requirements.txt pyinstaller || exit /b 1

echo Packing the Windows Python server...
"KnoxMap\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath desktop\pybuild --workpath desktop\pybuild\work desktop\knoxmap-server.spec || exit /b 1

if not exist "desktop\node_modules\electron-builder\cli.js" (
  echo Installing the window build tools...
  pushd desktop
  call npm install
  set ERR=%ERRORLEVEL%
  popd
  if not "%ERR%"=="0" exit /b %ERR%
)

echo Packing the Windows program...
pushd desktop
call node build.mjs --platform win32 --arch x64
set ERR=%ERRORLEVEL%
popd
if not "%ERR%"=="0" exit /b %ERR%

echo Packing the Windows command line...
"KnoxMap\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath desktop\pybuild --workpath desktop\pybuild\work-cli desktop\knoxmap-cli.spec || exit /b 1
set VER=0
for /f "tokens=2" %%v in ('findstr /b /c:"## " docs\CHANGELOG.md') do (
  set VER=%%v
  goto :gotver
)
:gotver
copy /y desktop\pybuild\knoxmap-cli.exe "release\KnoxMap-v%VER%-windows-cli.exe" >nul
echo Built %~dp0release\KnoxMap-v%VER%-windows-cli.exe

echo Packing the Linux and Mac programs...
docker run --rm -v "%CD%":/knoxmap -w /knoxmap node:22-bookworm bash desktop/build-foreign.sh
if errorlevel 1 exit /b 1

echo.
echo Built:
dir /b release
endlocal
