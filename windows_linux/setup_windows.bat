@echo off
rem Music Downloader - one-time setup for Windows. Afterwards, double-click MusicDownloader.pyw to start the app.
setlocal
cd /d "%~dp0"
echo Music Downloader - setup
echo.

where py >nul 2>nul
if errorlevel 1 goto nopython
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto nopython
py -3 -c "import tkinter" >nul 2>nul
if errorlevel 1 goto notk

echo Installing packages...
py -3 -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto nopip

where ffmpeg >nul 2>nul
if errorlevel 1 if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" goto noffmpeg
echo.
echo All set. Double-click MusicDownloader.pyw to start the app.
goto end

:noffmpeg
echo.
echo The packages are installed, but ffmpeg is not. It converts the songs. Install it with:
echo     winget install Gyan.FFmpeg
echo Then sign out of Windows and back in, and double-click MusicDownloader.pyw to start the app.
goto end

:nopython
echo Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/
echo (keep "py launcher" ticked in the installer), then run this again.
goto end

:notk
echo This Python was installed without Tk, the toolkit the window is drawn with.
echo Run the Python installer again, choose Modify, and tick "tcl/tk and IDLE".
goto end

:nopip
echo.
echo The packages could not be installed. Check your internet connection and run this again.

:end
echo.
pause
