@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ========================================
echo   更新雲端網站（3 步）
echo ========================================
echo.

if not exist "deploy_ready\app" (
  echo 正在準備 deploy_ready ...
  call "%~dp0pack_deploy.bat"
)

echo [1] 已開啟 deploy_ready 資料夾
explorer "%~dp0deploy_ready"

echo [2] 開啟 GitHub 上傳頁 — 請登入後:
echo     把 deploy_ready 裡「全部檔案與資料夾」拖進網頁
echo     下方 Commit changes
start "" "https://github.com/jay41004/baseball-analysis/upload/main"

ping 127.0.0.1 -n 5 >nul

echo [3] 開啟 Render — GitHub 上傳完後:
echo     點你的服務 → Manual Deploy → Deploy latest commit
start "" "https://dashboard.render.com/"

echo.
echo 雲端網址（目前已上線，更新後約 3 分鐘生效）:
echo   https://baseball-analysis.onrender.com/
echo   https://baseball-analysis.onrender.com/npb
echo.
pause
