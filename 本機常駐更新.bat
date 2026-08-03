@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ========================================
echo   本機常駐站（不依賴 Render 免費版）
echo ========================================
echo.
echo 會啟動 http://127.0.0.1:8000
echo 並每 3 小時完整更新一次 MLB/NPB/CPBL
echo 電腦需開著、可連網。
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo 找不到 python，請先安裝 Python 3.12+
  pause
  exit /b 1
)

start "baseball-web" cmd /c "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
ping 127.0.0.1 -n 4 >nul

:loop
echo [%date% %time%] 開始完整更新...
set CLOUD_LITE=0
set REFRESH_CONCURRENCY=2
python scripts/refresh_static_site.py
echo [%date% %time%] 更新結束，3 小時後再跑
ping 127.0.0.1 -n 10800 >nul
goto loop
