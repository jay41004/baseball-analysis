const DEFAULT_TEAM_ID = 1;
const STORAGE_KEY = "npb_last_team";
const REFRESH_MS = 60 * 60 * 1000;
const EXPECTED_CACHE_VERSION = 6;

let expectedCacheVersion = EXPECTED_CACHE_VERSION;

const teamSelect = document.getElementById("team-select");
const gameCountInput = document.getElementById("game-count");
const refreshBtn = document.getElementById("refresh-btn");
const loadingEl = document.getElementById("loading");
const errorEl = document.getElementById("error");
const resultsEl = document.getElementById("results");
const cacheStatusEl = document.getElementById("cache-status");
const matchupGridEl = document.getElementById("matchup-grid");

let hasDisplayedData = false;
let refreshTimer = null;

function pct(count, total) {
  if (!total) return "0%";
  return `${Math.round((count / total) * 100)}%`;
}

function formatTime(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("zh-TW", { hour12: false });
}

function formatGameTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("zh-TW", {
    timeZone: "Asia/Taipei",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function showLoading(show, message = "正在抓取 NPB 數據...") {
  loadingEl.textContent = message;
  loadingEl.classList.toggle("hidden", !show);
}

function showError(message) {
  if (!message) {
    errorEl.classList.add("hidden");
    errorEl.textContent = "";
    return;
  }
  errorEl.textContent = message;
  errorEl.classList.remove("hidden");
}

function updateCacheStatus(data) {
  let text = `資料更新：${formatTime(data.cachedAt)} · 下次自動更新：${formatTime(data.nextRefreshAt)}`;
  if (data.cacheVersion) text += ` · 快取 v${data.cacheVersion}`;
  if (data.cacheVersion && data.cacheVersion < 6) {
    text += " · 請按「立即更新」或 Ctrl+F5";
  }
  if (data.refreshing) text += " · 背景更新中…";
  cacheStatusEl.textContent = text;
}

function ouBadge(isOver) {
  return isOver
    ? '<span class="badge over">Over</span>'
    : '<span class="badge under">Under</span>';
}

function summaryCards(summary, totalLabel) {
  const firstInning = summary.firstInningScored ?? 0;
  const total = summary.totalGames ?? 0;
  return `
    <div class="summary-grid compact">
      <article class="stat-card">
        <span class="stat-label">Over 1.5</span>
        <strong class="stat-value over">${summary.over15}</strong>
        <span class="stat-sub">${pct(summary.over15, summary.totalGames)}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">Under 1.5</span>
        <strong class="stat-value under">${summary.under15}</strong>
        <span class="stat-sub">${pct(summary.under15, summary.totalGames)}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">Over 2.5</span>
        <strong class="stat-value over">${summary.over25}</strong>
        <span class="stat-sub">${pct(summary.over25, summary.totalGames)}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">Under 2.5</span>
        <strong class="stat-value under">${summary.under25}</strong>
        <span class="stat-sub">${pct(summary.under25, summary.totalGames)}</span>
      </article>
      <article class="stat-card highlight">
        <span class="stat-label">前五局平均得分</span>
        <strong class="stat-value">${summary.avgRuns}</strong>
        <span class="stat-sub">${totalLabel}</span>
      </article>
      <article class="stat-card highlight">
        <span class="stat-label">第一局得分</span>
        <strong class="stat-value over">${firstInning}</strong>
        <span class="stat-sub">${pct(firstInning, total)} · ${totalLabel}</span>
      </article>
    </div>
  `;
}

function teamFirstInningBadge(scored) {
  return scored
    ? '<span class="badge win">得分</span>'
    : '<span class="badge under">未得分</span>';
}

function inningScoredBadge(scored) {
  return scored
    ? '<span class="badge loss">掉分</span>'
    : '<span class="badge win">未掉分</span>';
}

function getScoredInnings(game) {
  if (game.scoredInnings?.length) return game.scoredInnings;
  if (game.runsByInning?.length) {
    return game.runsByInning
      .map((runs, index) => (runs > 0 ? index + 1 : null))
      .filter(Boolean);
  }
  return game.firstInningScored ? [1] : [];
}

function formatScoredInnings(game) {
  const scoredInnings = getScoredInnings(game);
  if (!scoredInnings.length) {
    return '<span class="muted-text">—</span>';
  }
  return `<span class="scored-innings">${scoredInnings.join("、")} 局</span>`;
}

function buildInningScoredCounts(summary, games) {
  const raw = summary?.inningScoredCounts;
  if (raw && Object.values(raw).some((value) => value > 0)) {
    return raw;
  }

  const counts = Object.fromEntries(
    [1, 2, 3, 4, 5, 6, 7, 8, 9].map((inning) => [String(inning), 0])
  );
  if (!games?.length) {
    return raw || counts;
  }

  for (const game of games) {
    const scored = new Set(game.scoredInnings || []);
    if (game.firstInningScored) scored.add(1);
    for (const inning of scored) {
      if (inning >= 1 && inning <= 9) counts[String(inning)] += 1;
    }
  }
  return counts;
}

function pitcherInningCountsGrid(summary, games) {
  const counts = buildInningScoredCounts(summary, games);
  const total = games?.length || summary.totalGames || 0;
  return `
    <div class="inning-counts-wrap">
      <p class="inning-counts-title">近 ${total} 次先發 · 各局掉分場數</p>
      <div class="inning-counts-grid">
        ${[1, 2, 3, 4, 5, 6, 7, 8, 9]
          .map((inning) => {
            const count = counts[String(inning)] ?? 0;
            return `
              <article class="inning-count-card ${count ? "has-runs" : ""}">
                <span class="inning-count-label">${inning}局</span>
                <strong class="inning-count-value">${count}</strong>
                <span class="inning-count-sub">${pct(count, total)}</span>
              </article>
            `;
          })
          .join("")}
      </div>
    </div>
  `;
}

function pitcherSummaryCards(summary, games) {
  const counts = buildInningScoredCounts(summary, games);
  const firstInningScored = counts["1"] ?? summary.firstInningScored ?? 0;
  const total = games?.length || summary.totalGames || 0;
  return `
    <div class="summary-grid compact">
      <article class="stat-card highlight span-2">
        <span class="stat-label">第 1 局掉分（守備半局）</span>
        <strong class="stat-value">${firstInningScored}</strong>
        <span class="stat-sub">${pct(firstInningScored, total)} · 近 ${total} 次先發</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">第 1 局未掉分</span>
        <strong class="stat-value">${total - firstInningScored}</strong>
        <span class="stat-sub">${pct(total - firstInningScored, total)}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">Over 1.5 失分</span>
        <strong class="stat-value over">${summary.over15}</strong>
        <span class="stat-sub">${pct(summary.over15, summary.totalGames)}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">Under 1.5 失分</span>
        <strong class="stat-value under">${summary.under15}</strong>
        <span class="stat-sub">${pct(summary.under15, summary.totalGames)}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">Over 2.5 失分</span>
        <strong class="stat-value over">${summary.over25}</strong>
        <span class="stat-sub">${pct(summary.over25, summary.totalGames)}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">Under 2.5 失分</span>
        <strong class="stat-value under">${summary.under25}</strong>
        <span class="stat-sub">${pct(summary.under25, summary.totalGames)}</span>
      </article>
      <article class="stat-card highlight span-2">
        <span class="stat-label">前五局平均失分</span>
        <strong class="stat-value">${summary.avgRuns}</strong>
        <span class="stat-sub">近 ${summary.totalGames} 次先發</span>
      </article>
    </div>
    ${pitcherInningCountsGrid(summary, games)}
  `;
}

function formatOpponent(game) {
  const prefix = game.isHome ? "vs" : "@";
  const starter = game.opponentStarter ? ` (${game.opponentStarter})` : "";
  return `${prefix} ${game.opponent}${starter}`;
}

function teamGamesTable(games) {
  if (!games.length) return `<p class="empty-note">尚無數據</p>`;
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>對手</th>
            <th>1局得分</th>
            <th>1–5 得分</th>
            <th>1.5</th>
            <th>2.5</th>
          </tr>
        </thead>
        <tbody>
          ${games
            .map(
              (game) => `
            <tr>
              <td>${game.date}</td>
              <td>${formatOpponent(game)}</td>
              <td>${teamFirstInningBadge(game.firstInningScored)}</td>
              <td class="runs">${game.firstFiveRuns}</td>
              <td>${ouBadge(game.over15)}</td>
              <td>${ouBadge(game.over25)}</td>
            </tr>
          `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function pitcherGamesTable(games) {
  if (!games.length) return `<p class="empty-note">尚無先發數據</p>`;
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>對手</th>
            <th>掉分局數</th>
            <th>1局失分</th>
            <th>1–5 失分</th>
            <th>1.5</th>
            <th>2.5</th>
          </tr>
        </thead>
        <tbody>
          ${games
            .map(
              (game) => `
            <tr>
              <td>${game.date}</td>
              <td>${formatOpponent(game)}</td>
              <td>${formatScoredInnings(game)}</td>
              <td>${inningScoredBadge(game.firstInningScored)} <span class="runs">${game.firstInningRunsAllowed}</span></td>
              <td class="runs">${game.firstFiveRunsAllowed}</td>
              <td>${ouBadge(game.over15)}</td>
              <td>${ouBadge(game.over25)}</td>
            </tr>
          `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderSideColumn(side, role) {
  const isAway = role === "客隊";
  const pitcherName = side.probablePitcher?.fullName ?? "尚未公布";
  const pitcher = side.pitcherAnalysis;
  const totalLabel = `近 ${side.summary.totalGames} 場`;

  return `
    <article class="team-column ${isAway ? "away-column" : "home-column"}">
      <div class="column-head">
        <span class="role-badge ${isAway ? "away-badge" : "home-badge"}">${role}</span>
        <h2>${side.teamName}</h2>
        <p class="pitcher-line">先發投手：<span class="pitcher-name">${pitcherName}</span></p>
      </div>

      <section class="panel-block">
        <h3>近 ${side.summary.totalGames} 場 · 1–5 局得分</h3>
        ${summaryCards(side.summary, totalLabel)}
        ${teamGamesTable(side.games)}
      </section>

      <section class="panel-block">
        <p class="pitcher-section-name">${pitcher?.pitcherName ?? pitcherName}</p>
        <h3>先發投手 · 近 ${pitcher?.games?.length ?? pitcher?.summary?.totalGames ?? 10} 次先發（逐局掉分 / 1–5 局失分）</h3>
        ${
          pitcher && pitcher.games.length
            ? `${pitcherSummaryCards(pitcher.summary, pitcher.games)}${pitcherGamesTable(pitcher.games)}`
            : `<p class="empty-note">${pitcherName === "尚未公布" ? "NPB 尚未公布先發投手" : "找不到此投手近期先發數據"}</p>`
        }
      </section>
    </article>
  `;
}

function renderMatchup(data) {
  const { matchup, away, home } = data;

  document.getElementById("matchup-title").textContent = `${away.teamName} @ ${home.teamName}`;
  const taiwanTime = formatGameTime(matchup.gameDate);
  const metaParts = [matchup.date];
  if (matchup.stadium) metaParts.push(matchup.stadium.replace(/\s+/g, " ").trim());
  if (taiwanTime) metaParts.push(`台灣 ${taiwanTime}`);
  metaParts.push(matchup.status || "Scheduled");
  document.getElementById("matchup-meta").textContent = metaParts.join(" · ");

  matchupGridEl.innerHTML = `
    ${renderSideColumn(away, "客隊")}
    ${renderSideColumn(home, "主隊")}
  `;

  updateCacheStatus(data);
}

async function loadTeams() {
  const resp = await fetch("/api/npb/teams");
  if (!resp.ok) throw new Error("無法載入球隊列表");
  const teams = await resp.json();

  teamSelect.innerHTML = teams
    .map((team) => {
      const league = team.league === "CL" ? "セ" : "パ";
      return `<option value="${team.id}">${team.nameZh} (${league})</option>`;
    })
    .join("");

  const savedTeam = localStorage.getItem(STORAGE_KEY);
  teamSelect.value = savedTeam || String(DEFAULT_TEAM_ID);
}

function needsFreshData(data, games) {
  if (!data.cacheVersion || data.cacheVersion < expectedCacheVersion) {
    return true;
  }
  for (const side of ["away", "home"]) {
    const pitcher = data[side]?.pitcherAnalysis;
    if (!pitcher?.pitcherName || !pitcher.games?.length) {
      continue;
    }
    const target = Math.min(games, 10);
    if (pitcher.games.length < target) {
      return true;
    }
    const summaryTotal = pitcher.summary?.totalGames ?? 0;
    if (summaryTotal > 0 && summaryTotal !== pitcher.games.length) {
      return true;
    }
  }
  return false;
}

async function fetchAnalysis(force = false, allowAutoRetry = true) {
  const teamId = teamSelect.value;
  const games = Number(gameCountInput.value) || 10;
  if (!teamId) return;

  localStorage.setItem(STORAGE_KEY, teamId);
  showError("");

  const firstLoad = !hasDisplayedData;
  if (firstLoad) {
    showLoading(true);
    resultsEl.classList.add("hidden");
  } else if (force) {
    cacheStatusEl.textContent = "正在更新資料…";
  }

  try {
    const query = new URLSearchParams({ team_id: teamId, games: String(games) });
    if (force) query.set("force", "true");

    const resp = await fetch(`/api/npb/matchup?${query}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "載入失敗");

    if (!force && allowAutoRetry && needsFreshData(data, games)) {
      cacheStatusEl.textContent = "偵測到舊資料，正在自動更新…";
      return fetchAnalysis(true, false);
    }

    renderMatchup(data);
    resultsEl.classList.remove("hidden");
    hasDisplayedData = true;

    if (data.refreshing && !force) scheduleSilentRefresh();
  } catch (err) {
    if (err.message === "Failed to fetch" || err.name === "TypeError") {
      showError("無法連線。請確認 run.bat 已執行且視窗保持開啟。");
    } else {
      showError(err.message || "發生未知錯誤");
    }
  } finally {
    showLoading(false);
  }
}

function scheduleSilentRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => fetchAnalysis(false), 20000);
}

function scheduleHourlyRefresh() {
  setInterval(() => fetchAnalysis(false), REFRESH_MS);
}

async function loadMeta() {
  try {
    const resp = await fetch("/api/meta");
    if (resp.ok) {
      const meta = await resp.json();
      if (meta.npbCacheVersion) expectedCacheVersion = meta.npbCacheVersion;
    }
  } catch (_) {
    /* server offline */
  }
}

refreshBtn.addEventListener("click", () => fetchAnalysis(true));
teamSelect.addEventListener("change", () => fetchAnalysis(false));
gameCountInput.addEventListener("change", () => fetchAnalysis(false));

loadTeams()
  .then(() => loadMeta())
  .then(() => fetchAnalysis(false))
  .then(() => scheduleHourlyRefresh())
  .catch((err) => showError(err.message));
