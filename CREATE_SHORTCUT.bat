@echo off
setlocal EnableDelayedExpansion
:: ── Find pythonw.exe ──────────────────────────────────────────────────────────
for /f "delims=" %%i in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set PYTHONW=%%i
if not defined PYTHONW (
    echo ERROR: Python not found. Make sure Python is installed and on PATH.
    pause & exit /b 1
)
if not exist "%PYTHONW%" (
    echo ERROR: pythonw.exe not found at: %PYTHONW%
    pause & exit /b 1
)
:: ── Paths ─────────────────────────────────────────────────────────────────────
set "SCRIPT=%~dp0whisper_hotkey.py"
set "ICON=%~dp0whisper_hotkey.ico"
set "WORKDIR=%~dp0"
if "%WORKDIR:~-1%"=="\" set "WORKDIR=%WORKDIR:~0,-1%"
if not exist "%SCRIPT%" (
    echo ERROR: whisper_hotkey.py not found in the same folder as this batch file.
    pause & exit /b 1
)
:: Fallback to pythonw icon if .ico is missing
if not exist "%ICON%" set "ICON=%PYTHONW%"
set "DESKTOP=%USERPROFILE%\Desktop"
set "LINK=%DESKTOP%\Whisper Hotkey.lnk"
set "STARTMENU_LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Whisper Hotkey.lnk"
:: ── Write a temporary VBScript and run it ────────────────────────────────────
set "VBS=%TEMP%\mk_whisper_link.vbs"
(
echo Set oShell = CreateObject("WScript.Shell"^)
echo.
echo Set oLink = oShell.CreateShortcut("%LINK%"^)
echo oLink.TargetPath       = "%PYTHONW%"
echo oLink.Arguments        = """%SCRIPT%"""
echo oLink.WorkingDirectory = "%WORKDIR%"
echo oLink.WindowStyle      = 7
echo oLink.IconLocation     = "%ICON%,0"
echo oLink.Description      = "Whisper Hotkey - speech-to-text dictation"
echo oLink.Save
echo.
echo Set oLink2 = oShell.CreateShortcut("%STARTMENU_LINK%"^)
echo oLink2.TargetPath       = "%PYTHONW%"
echo oLink2.Arguments        = """%SCRIPT%"""
echo oLink2.WorkingDirectory = "%WORKDIR%"
echo oLink2.WindowStyle      = 7
echo oLink2.IconLocation     = "%ICON%,0"
echo oLink2.Description      = "Whisper Hotkey - speech-to-text dictation"
echo oLink2.Save
) > "%VBS%"
cscript //nologo "%VBS%"
del "%VBS%" 2>nul
:: ── Result ────────────────────────────────────────────────────────────────────
if exist "%LINK%" (
    echo.
    echo  Shortcut created successfully!
    echo  Desktop Location: %LINK%
    echo  Start Menu Location: %STARTMENU_LINK%
    echo.
) else (
    echo.
    echo  ERROR: Shortcut was not created. Try running this file as Administrator.
    echo.
)
pause