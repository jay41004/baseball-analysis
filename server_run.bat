@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [%date% %time%] Starting server...>> server.log
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload >> server.log 2>&1
echo [%date% %time%] Server exited with code %errorlevel%>> server.log
