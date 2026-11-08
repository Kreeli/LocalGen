@echo off
setlocal
set "ROOT=%~dp0"
set "PORTABLE=%ROOT%.runtime\ComfyUI_windows_portable"
if not exist "%PORTABLE%\ComfyUI\main.py" set "PORTABLE="
if exist "%LOCALAPPDATA%\LocalGen\config.toml" set "PORTABLE="
if exist "%ROOT%dist\LocalGen\LocalGen.exe" (
    if defined PORTABLE (
        start "" "%ROOT%dist\LocalGen\LocalGen.exe" --engine-root "%PORTABLE%"
    ) else (
        start "" "%ROOT%dist\LocalGen\LocalGen.exe"
    )
    exit /b
)
if exist "%ROOT%.venv\Scripts\pythonw.exe" (
    if defined PORTABLE (
        start "" "%ROOT%.venv\Scripts\pythonw.exe" -m localgen --engine-root "%PORTABLE%"
    ) else (
        start "" "%ROOT%.venv\Scripts\pythonw.exe" -m localgen
    )
    exit /b
)
echo LocalGen is not installed. See README.md.
pause
