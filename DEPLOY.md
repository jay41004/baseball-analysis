# 雲端部署指南（GitHub Pages + Render 雙軌）

**手機主站（完整數據，定時更新）：** https://jay41004.github.io/baseball-analysis/  
**即時 API（Render）：** https://baseball-analysis.onrender.com  

---

## 兩邊怎麼分工（修好後不要再混用角色）

| | GitHub Pages | Render |
|--|--|--|
| 用途 | **手機日常看** | 需要即時刷新 / 本機以外的 API |
| 資料 | Actions 每 4 小時完整重建 | 從 Pages **同步面板** + 即時更新「下一場」標頭 |
| 不休眠 | 是 | 免費版約 15 分鐘會睡 |

兩邊會對齊：Render 沒資料時會先拉 Pages；有人打開時再更新下一場日期／先發。

---

## 最快更新方式（本機改好程式）

1. 雙擊 **`一鍵上傳.bat`**（會打包 `deploy_ready` 並開啟上傳頁）
2. 把 `deploy_ready` 裡**全部**拖到 GitHub → Commit
3. GitHub → **Actions** → `Refresh data and deploy GitHub Pages` → **Run workflow**
4. Render → **Manual Deploy** → Deploy latest commit
5. 確認 `https://baseball-analysis.onrender.com/api/meta` 的 `deployMark` 為最新

---

## 你需要準備

1. [GitHub](https://github.com) 帳號  
2. [Render](https://render.com) 帳號（可用 GitHub 登入）  
3. Repo：`jay41004/baseball-analysis`（已連 Render）

**一定要上傳：** `app/`、`static/`、`templates/`、`scripts/`、`.github/`、`requirements.txt`、`render.yaml`、`.gitignore`、`data/.gitkeep`（可含 `data/cpbl_schedule.json`）

**不要上傳：** 本機除錯檔、`probe_*`、巨大舊 `cache.json`（會讓 Render 卡在過期場次）

---

## 免費版注意

| 項目 | 說明 |
|------|------|
| Render 休眠 | 約 15 分鐘沒人訪問會睡，第一次可能等 30～60 秒 |
| Pages | 不依賴 Render；手機優先開 GitHub 站 |
| 確認就緒 | Pages：`/data/meta.json`；Render：`/api/meta` 且 `deployMark` 正確 |

---

## 常見問題

**Q：一邊對、一邊錯？**  
先確認兩邊程式都是同一版（上傳完整 `deploy_ready` + Render redeploy + 跑過 Pages Refresh）。

**Q：Render 還顯示很舊的場次？**  
舊 seed 快取；新版會從 Pages 同步並刷新標頭。強制開一次該隊頁面或等 redeploy 後再試。

**Q：部署失敗？**  
Render → Logs；GitHub → Actions 看 Refresh 是否失敗。
