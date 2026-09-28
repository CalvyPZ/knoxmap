@echo off
rem Build the player downloads into releases\ (gitignored):
rem   KnoxMap-v…-windows.exe       window
rem   KnoxMap-v…-windows-cli.exe   command line, no window
rem   KnoxMap-v…-linux.AppImage
rem   KnoxMap-v…-linux-cli
rem   KnoxMap-v…-macos.zip     (KnoxMap.app, plus knoxmap-cli beside it)
rem
rem Windows is packed on this PC. Linux and Mac are packed in Docker.
rem PyInstaller scratch is releases\temp. The window packager's files are
rem moved to releases\dist. Needs 64-bit Python 3.10+, Node.js, and Docker.
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

rem python3 is the normal interpreter. python on this PC is the free-threaded
rem build, and SciPy never finishes a room layout there.
where python3 >nul 2>nul || (
  echo Need the python3 command. python is free-threaded and SciPy does not run on it.
  exit /b 1
)
findstr /i /c:"python3.14t" /c:"freethreaded" "KnoxMap\.venv\pyvenv.cfg" >nul 2>nul && (
  echo The Python environment is free-threaded. Recreating it with python3...
  rmdir /s /q "KnoxMap\.venv"
)
if not exist "KnoxMap\.venv\Scripts\python.exe" (
  echo Creating the Python environment with python3...
  python3 -m venv KnoxMap\.venv || exit /b 1
)

if not exist "releases\temp" mkdir "releases\temp"
rem The window packager reads desktop\pybuild. The files themselves are in
rem releases\temp; the junction only exists so that lookup still works.
if exist "desktop" if not exist "desktop\pybuild" mklink /J "desktop\pybuild" "%CD%\releases\temp" >nul

echo Installing Python packages...
"KnoxMap\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r KnoxMap\requirements.txt pyinstaller || exit /b 1

echo Packing the Windows Python server...
"KnoxMap\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath releases\temp --workpath releases\temp\work desktop\knoxmap-server.spec || exit /b 1

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
"KnoxMap\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath releases\temp --workpath releases\temp\work-cli desktop\knoxmap-cli.spec || exit /b 1
set VER=0
for /f "tokens=2" %%v in ('findstr /b /c:"## " docs\CHANGELOG.md') do (
  set VER=%%v
  goto :gotver
)
:gotver
copy /y "releases\temp\knoxmap-cli.exe" "releases\KnoxMap-v%VER%-windows-cli.exe" >nul
echo Built %~dp0releases\KnoxMap-v%VER%-windows-cli.exe

echo Packing the Linux and Mac programs...
docker run --rm -v "%CD%":/knoxmap -w /knoxmap node:22-bookworm bash desktop/build-foreign.sh
if errorlevel 1 exit /b 1

rem Packager scripts still drop the window build in desktop\out and may copy
rem named downloads to release\ or released\. Keep all of that under releases\.
if exist "desktop\out" (
  if not exist "releases\dist" mkdir "releases\dist"
  robocopy "desktop\out" "releases\dist" /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np >nul
  rmdir "desktop\out" 2>nul
)
for %%d in (release released) do if exist "%%d" for %%f in ("%%d\*") do (
  if /I not "%%~nxf"==".gitkeep" move /y "%%f" "releases\" >nul
)

echo.
echo Built:
dir /b releases
endlocal
