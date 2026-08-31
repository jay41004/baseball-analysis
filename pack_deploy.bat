@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ========================================
echo   打包雲端部署檔（上傳 GitHub 用）
echo ========================================
echo.

set "OUT=deploy_upload.zip"
if exist "%OUT%" del "%OUT%"

powershell -NoProfile -Command ^
  "$root = '%CD%';" ^
  "$staging = Join-Path $root 'deploy_staging';" ^
  "if (Test-Path $staging) { Remove-Item $staging -Recurse -Force };" ^
  "New-Item -ItemType Directory -Path $staging | Out-Null;" ^
  "Copy-Item -Recurse (Join-Path $root 'app') (Join-Path $staging 'app');" ^
  "Copy-Item -Recurse (Join-Path $root 'static') (Join-Path $staging 'static');" ^
  "Copy-Item -Recurse (Join-Path $root 'templates') (Join-Path $staging 'templates');" ^
  "if (Test-Path (Join-Path $root 'scripts')) { Copy-Item -Recurse (Join-Path $root 'scripts') (Join-Path $staging 'scripts') };" ^
  "New-Item -ItemType Directory -Path (Join-Path $staging 'data') | Out-Null;" ^
  "Copy-Item (Join-Path $root 'data\.gitkeep') (Join-Path $staging 'data\.gitkeep') -ErrorAction SilentlyContinue;" ^
  "$seed = @('cpbl_schedule.json');" ^
  "foreach ($f in $seed) { $src = Join-Path $root ('data\' + $f); if (Test-Path $src) { Copy-Item $src (Join-Path $staging 'data') } };" ^
  "Copy-Item (Join-Path $root 'requirements.txt') $staging;" ^
  "Copy-Item (Join-Path $root 'render.yaml') $staging;" ^
  "Copy-Item (Join-Path $root 'Dockerfile') $staging -ErrorAction SilentlyContinue;" ^
  "Copy-Item (Join-Path $root '.gitignore') $staging;" ^
  "Copy-Item (Join-Path $root 'DEPLOY.md') $staging -ErrorAction SilentlyContinue;" ^
  "if (Test-Path (Join-Path $root '.github')) { Copy-Item -Recurse (Join-Path $root '.github') (Join-Path $staging '.github') };" ^
  "Compress-Archive -Path (Join-Path $staging '*') -DestinationPath (Join-Path $root '%OUT%') -Force;" ^
  "Remove-Item $staging -Recurse -Force"

if not exist "%OUT%" (
  echo 打包失敗。
  pause
  exit /b 1
)

echo.
echo 已建立: %OUT%
echo.
if exist "deploy_ready" rmdir /s /q "deploy_ready"
mkdir "deploy_ready"
powershell -NoProfile -Command "Expand-Archive -Path '%OUT%' -DestinationPath 'deploy_ready' -Force"
echo 已解壓到: deploy_ready\
echo.
echo 【角色分工 — 兩邊一起用、不要互相搶】
echo   GitHub Pages = 手機主站（每幾小時完整更新數據）
echo   Render       = 即時 API（從 Pages 同步 + 更新下一場標頭）
echo.
echo 【下一步】
echo   1. 雙擊 deploy_now.bat 或把 deploy_ready 全部拖上 GitHub
echo   2. GitHub → Actions → Refresh data... → Run workflow
echo   3. Render → Manual Deploy → Deploy latest commit
echo.
explorer "%~dp0deploy_ready"
echo.
pause
