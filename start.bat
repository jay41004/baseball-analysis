@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo 棒球前五局分析 - 啟動中...

set "PORT=8000"
set "DEPLOY_MARK=2026-09-24-pitcher-force-sync"

call :kill_port 8000
call :kill_port 8001

python -c "import urllib.request,json,sys; m=json.loads(urllib.request.urlopen('http://127.0.0.1:8000/api/meta',timeout=2).read()); sys.exit(0 if m.get('deployMark')=='%DEPLOY_MARK%' else 1)" >nul 2>&1
if %errorlevel%==0 (
  echo 新版伺服器已在 8000 運行。
  goto :ready
)

echo 正在背景啟動新版伺服器（port %PORT%）...
start "" /B cmd /c "python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% >> server.log 2>&1"

set /a tries=0
:wait_loop
ping 127.0.0.1 -n 2 >nul
python -c "import urllib.request,json,sys; m=json.loads(urllib.request.urlopen('http://127.0.0.1:%PORT%/api/meta',timeout=2).read()); sys.exit(0 if m.get('deployMark')=='%DEPLOY_MARK%' else 1)" >nul 2>&1
if %errorlevel%==0 goto :ready
set /a tries+=1
if %tries% lss 15 goto :wait_loop

if "%PORT%"=="8000" (
  echo 8000 被舊程式占用，改啟動 8001...
  set "PORT=8001"
  start "" /B cmd /c "python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% >> server.log 2>&1"
  set /a tries=0
  :wait_loop_8001
  ping 127.0.0.1 -n 2 >nul
  python -c "import urllib.request,json,sys; m=json.loads(urllib.request.urlopen('http://127.0.0.1:%PORT%/api/meta',timeout=2).read()); sys.exit(0 if m.get('deployMark')=='%DEPLOY_MARK%' else 1)" >nul 2>&1
  if %errorlevel%==0 goto :ready
  set /a tries+=1
  if %tries% lss 15 goto :wait_loop_8001
)

echo.
echo 啟動失敗。請執行 restart.bat 或看 server.log
pause
exit /b 1

:ready
echo.
echo 網站已就緒（新版）:
echo   http://127.0.0.1:%PORT%
echo   http://127.0.0.1:%PORT%/npb
echo.
echo 伺服器在背景運行，此視窗可關閉。
ping 127.0.0.1 -n 4 >nul
exit /b 0

:kill_port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%1" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)
exit /b 0
