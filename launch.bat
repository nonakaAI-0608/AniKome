@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. > startup.log
    exit /b 1
)
uv run --no-project --python 3.11 --with tkinterdnd2 python nico_ass_tool.py > startup.log 2>&1
exit /b %errorlevel%
