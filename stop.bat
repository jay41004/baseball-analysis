@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 停止棒球分析伺服器...

for %%P in (8000 8001 8002) do (
  for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do (
    echo 終止 port %%P PID %%a
    taskkill /F /PID %%a >nul 2>&1
  )
)

echo 已停止（若原本沒在跑則略過）。
ping 127.0.0.1 -n 3 >nul
