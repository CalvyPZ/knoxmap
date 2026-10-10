@echo off
rem Build the macOS download into releases\ (gitignored):
rem   KnoxMap-v…-macos.zip     (KnoxMap.app, plus knoxmap-cli beside it)
rem
rem The arm64 helper library and map compiler are cross-built in Docker, then the app is packed
rem in Docker. A dmg needs Apple's tools, which this PC does not have.
rem Needs Docker Desktop.
setlocal
cd /d "%~dp0"

where docker >nul 2>nul || (
  echo Docker is not installed. Install Docker Desktop, then run this again.
  exit /b 1
)

echo Building the macOS helper library...
docker run --rm --entrypoint bash -v "%CD%":/io -w /io ghcr.io/rust-cross/cargo-zigbuild desktop/build-macos-libs.sh
if errorlevel 1 (
  echo The macOS helper library did not build.
  exit /b 1
)

echo Packing the Mac program...
docker run --rm -v "%CD%":/knoxmap -w /knoxmap node:22-bookworm bash desktop/build-macos.sh
if errorlevel 1 exit /b 1

echo.
echo Built:
dir /b releases\*macos*
endlocal
