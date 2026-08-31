@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo 棒球前五局分析 - 啟動中...

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo 伺服器已在背景運行。
  echo 若剛改過程式，請先執行 stop.bat 再 start.bat，或瀏覽器 Ctrl+F5。
  goto :open_browser
)

echo 正在背景啟動伺服器...
wscript.exe "%~dp0server_hidden.vbs"

set /a tries=0
:wait_loop
ping 127.0.0.1 -n 2 >nul
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto :open_browser
set /a tries+=1
if %tries% lss 12 goto :wait_loop

echo.
echo 啟動失敗。請依序檢查:
echo   1. 命令列輸入 python --version 是否正常
echo   2. 執行: pip install -r requirements.txt
echo   3. 若 8000 被占用，先執行 stop.bat 再重試
echo.
if exist "%~dp0server.log" (
  echo --- server.log 最後幾行 ---
  powershell -NoProfile -Command "Get-Content -Path '%~dp0server.log' -Tail 15 -Encoding UTF8"
  echo -----------------------------
)
echo.
pause
exit /b 1

:open_browser
echo.
echo 網站已就緒:
echo   http://127.0.0.1:8000
echo   http://127.0.0.1:8000/npb
echo.
start "" "http://127.0.0.1:8000"
echo 伺服器在背景運行，此視窗可關閉。
ping 127.0.0.1 -n 4 >nul
exit /b 0
