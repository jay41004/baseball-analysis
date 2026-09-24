(function () {
  const root = document.getElementById("slate-root");
  const statusEl = document.getElementById("slate-status");
  const errorEl = document.getElementById("slate-error");

  const LEAGUE_LABEL = { mlb: "MLB 美國職棒", npb: "NPB 日本職棒", cpbl: "CPBL 中華職棒" };
  const LEAGUE_ORDER = ["npb", "cpbl", "mlb"];

  function showError(msg) {
    errorEl.textContent = msg || "";
    errorEl.classList.toggle("hidden", !msg);
  }

  function formatPitchers(row) {
    const away = row.awayPitcher || "待定";
    const home = row.homePitcher || "待定";
    if (!row.awayPitcher && !row.homePitcher) return "先發待定";
    return `${away} vs ${home}`;
  }

  function renderRows(rows) {
    if (!rows.length) {
      return '<p class="lineup-note">本日無賽事</p>';
    }
    return `
      <div class="slate-list">
        ${rows
          .map(
            (row) => `
          <a class="slate-card card" href="${row.analysisUrl}">
            <div class="slate-card-top">
              <span class="slate-time">${row.timeTaiwan ? `台灣 ${row.timeTaiwan}` : row.date}</span>
              <span class="slate-status-badge">${row.status || "Scheduled"}</span>
            </div>
            <div class="slate-matchup">
              <span class="team-name-away">${row.awayName}</span>
              <span class="at-symbol">@</span>
              <span class="team-name-home">${row.homeName}</span>
            </div>
            <p class="slate-meta">${[row.stadium, formatPitchers(row)].filter(Boolean).join(" · ")}</p>
          </a>
        `
          )
          .join("")}
      </div>
    `;
  }

  function renderLeague(name, data) {
    return `
      <section class="card slate-section">
        <h2 class="slate-league-title">${LEAGUE_LABEL[name] || name}</h2>
        <h3 class="slate-day-title">今日（${data.todayLabel || ""}）</h3>
        ${renderRows(data.today || [])}
        <h3 class="slate-day-title">明日（${data.tomorrowLabel || ""}）</h3>
        ${renderRows(data.tomorrow || [])}
      </section>
    `;
  }

  function renderAll(payload) {
    const today = payload.today || "";
    const tomorrow = payload.tomorrow || "";
    const sections = LEAGUE_ORDER.map((league) =>
      renderLeague(league, {
        today: payload[league]?.today || [],
        tomorrow: payload[league]?.tomorrow || [],
        todayLabel: today,
        tomorrowLabel: tomorrow,
      })
    ).join("");
    root.innerHTML = sections;
    statusEl.textContent = `更新：${new Date(payload.generatedAt || Date.now()).toLocaleString("zh-TW", { hour12: false })}`;
  }

  async function load() {
    showError("");
    try {
      const url = SiteConfig.isStatic ? SiteConfig.dataUrl("meta", "../meta.json") : SiteConfig.api("/api/slate");
      if (SiteConfig.isStatic) {
        showError("靜態站請用本機或 Render 開啟此頁。");
        statusEl.textContent = "";
        return;
      }
      const { resp, data } = await ApiUtils.fetchJson(url, (u) => fetch(u));
      if (!resp.ok) throw new Error(data.detail || "載入失敗");
      renderAll(data);
    } catch (err) {
      showError(err.message || "無法載入賽程");
      statusEl.textContent = "";
    }
  }

  load();
})();
