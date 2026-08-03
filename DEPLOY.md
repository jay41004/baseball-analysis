# 雲端部署指南

**手機主站（建議固定用這個）：** https://jay41004.github.io/baseball-analysis/  
**Render 備援：** https://baseball-analysis.onrender.com  

穩定度與「自己架站」請看 **[可靠度說明.md](可靠度說明.md)**。

---

## 兩邊角色（不要混）

| | GitHub Pages | Render 免費 |
|--|--|--|
| 角色 | **主站**，每 2 小時完整更新 | 備援；資料從 Pages 同步 |
| 不休眠 | 是 | 否（約 15 分鐘沒人會睡） |

---

## 更新程式碼

```bat
git add -A
git commit -m "你的說明"
git push origin main
```

資料不會因每次 push 自動重抓（避免更新互相取消）。  
要立刻刷新數據：GitHub → **Actions** → `Refresh data and deploy GitHub Pages` → **Run workflow**。

或雙擊 `一鍵上傳.bat`（無 Git 時）。

---

## 自己架常駐站（較不易壞）

- Windows：雙擊 **`本機常駐更新.bat`**
- VPS：`docker compose up -d --build`（見 `可靠度說明.md`）

---

## 確認是否正常

- Pages：https://jay41004.github.io/baseball-analysis/data/meta.json  
  看 `generatedAt` 是否為近幾小時內，三聯盟數量夠（MLB≈30、NPB≈12、CPBL≈6）
- Render：https://baseball-analysis.onrender.com/api/meta  
  `deployMark` 應含 `dual-sync`
