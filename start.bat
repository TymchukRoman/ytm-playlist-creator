@echo off
setlocal
title YT Music Sorter
cd /d "%~dp0"

rem uv installs itself into this folder; add it to PATH in case it is installed but not on PATH yet.
set "PATH=%PATH%;%USERPROFILE%\.local\bin"

where uv >nul 2>nul
if not errorlevel 1 goto run

echo First start: installing uv (a small helper that sets up everything else)...
echo.
powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%PATH%;%USERPROFILE%\.local\bin"
where uv >nul 2>nul
if errorlevel 1 goto uvfail

:run
echo Starting YT Music Sorter. The first start downloads a few things and can take a minute.
echo.
uv run --frozen ytsorter
if errorlevel 1 goto failed
exit /b 0

:uvfail
echo.
echo Sorry, installing uv did not work. Check your internet connection and try again.
echo If it keeps failing, install uv yourself from
echo https://docs.astral.sh/uv/getting-started/installation/ and run start.bat again.
echo.
pause
exit /b 1

:failed
echo.
echo Sorry, YT Music Sorter could not start or stopped because of a problem.
echo Check your internet connection and try again. If it keeps happening, take a
echo screenshot of this window and see "Troubleshooting" in the README.
echo.
pause
exit /b 1
