@echo off
python -m pip install fastapi uvicorn websockets pywebview
if errorlevel 1 exit /b 1
python launcher.py
