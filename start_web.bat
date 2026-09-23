@echo off
cd /d "%~dp0"
start "GPU Orchestrator Web" /min python app.py
timeout /t 2 /nobreak >nul
start http://127.0.0.1:8765/
