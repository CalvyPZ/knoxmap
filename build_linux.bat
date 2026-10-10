@echo off
rem Build the Linux downloads into releases\ (gitignored):
rem   KnoxMap-v…-linux.AppImage
rem   KnoxMap-v…-linux-cli
rem
rem Packed in Docker, which also builds libhelper.so. Needs Docker Desktop.
setlocal
cd /d "%~dp0"

where docker >nul 2>nul || (
  echo Docker is not installed. Install Docker Desktop, then run this again.
  exit /b 1
)

echo Packing the Linux program...
docker run --rm -v "%CD%":/knoxmap -w /knoxmap node:22-bookworm bash desktop/build-linux.sh
if errorlevel 1 exit /b 1

echo.
echo Built:
dir /b releases\*linux*
endlocal
