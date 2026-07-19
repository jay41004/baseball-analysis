const PHILADELPHIA_ID = 143;
const STORAGE_KEY = "mlb_last_team";
const REFRESH_MS = 60 * 60 * 1000;
const POLL_MS = 3000;
const REFRESH_POLL_MS = 8000;
const EXPECTED_CACHE_VERSION = 10;

const FETCH_TIMEOUT_MS = 45000;
const MAX_POLL_ATTEMPTS = 15;

let expectedCacheVersion = EXPECTED_CACHE_VERSION;
let pollTimer = null;
let hourlyTimer = null;
let fetchInFlight = false;
let pollAttempts = 0;

const teamSelect = document.getElementById("team-select");
const gameCountInput = document.getElementById("game-count");
const refreshBtn = document.getElementById("refresh-btn");
const loadingEl = document.getElementById("loading");
const errorEl = document.getElementById("error");
const resultsEl = document.getElementById("results");
const cacheStatusEl = document.getElementById("cache-status");
const matchupGridEl = document.getElementById("matchup-grid");

let hasDisplayedData = false;
let lastContentFingerprint = "";
let fetchToken = 0;

function buildContentFingerprint(data) {
  const { matchup, away, home, cachedAt } = data;
  return JSON.stringify({ cachedAt, matchup, away, home });
}

function captureTableScroll() {
  return [...document.querySelectorAll(".table-wrap")].map((el) => el.scrollLeft);
}

function restoreTableScroll(positions) {
  document.querySelectorAll(".table-wrap").forEach((el, index) => {
    if (positions[index] != null) el.scrollLeft = positions[index];
  });
}

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

function showLoading(show, message = "正在抓取 MLB 數據...") {
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
  if (data.refreshing) text += " · 背景更新中…";
  cacheStatusEl.textContent = text;
}

function ouBadge(isOver) {
  return isOver
    ? '<span class="badge over">Over</span>'
    : '<span class="badge under">Under</span>';
}

function resultBadge(isWin) {
  return isWin
    ? '<span class="badge win">W</span>'
    : '<span class="badge loss">L</span>';
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
  const total = summary.totalGames || 0;
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
  const total = summary.totalGames || 0;
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
      <table class="data-table">
        <thead>
          <tr>
            <th class="col-date">日期</th>
            <th class="col-opp">對手</th>
            <th class="col-num">1局得分</th>
            <th class="col-num">1–5 得分</th>
            <th class="col-ou">1.5</th>
            <th class="col-ou">2.5</th>
          </tr>
        </thead>
        <tbody>
          ${games
            .map(
              (game) => `
            <tr>
              <td class="col-date">${game.date.slice(5)}</td>
              <td class="col-opp">${formatOpponent(game)}</td>
              <td class="col-num">${teamFirstInningBadge(game.firstInningScored)}</td>
              <td class="col-num runs">${game.firstFiveRuns}</td>
              <td class="col-ou">${ouBadge(game.over15)}</td>
              <td class="col-ou">${ouBadge(game.over25)}</td>
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
      <table class="data-table">
        <thead>
          <tr>
            <th class="col-date">日期</th>
            <th class="col-opp">對手</th>
            <th class="col-num">掉分局數</th>
            <th class="col-num">1局失分</th>
            <th class="col-num">1–5 失分</th>
            <th class="col-ou">1.5</th>
            <th class="col-ou">2.5</th>
          </tr>
        </thead>
        <tbody>
          ${games
            .map(
              (game) => `
            <tr>
              <td class="col-date">${game.date.slice(5)}</td>
              <td class="col-opp">${formatOpponent(game)}</td>
              <td class="col-num">${formatScoredInnings(game)}</td>
              <td class="col-num">${inningScoredBadge(game.firstInningScored)} <span class="runs">${game.firstInningRunsAllowed}</span></td>
              <td class="col-num runs">${game.firstFiveRunsAllowed}</td>
              <td class="col-ou">${ouBadge(game.over15)}</td>
              <td class="col-ou">${ouBadge(game.over25)}</td>
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
  const pitcherDisplayName = pitcher?.pitcherName ?? pitcherName;
  const gameCount = side.summary.totalGames ?? 0;
  const startCount = pitcher?.games?.length ?? pitcher?.summary?.totalGames ?? 10;
  const totalLabel = `${side.teamName} 近 ${gameCount} 場`;
  const teamClass = isAway ? "team-name-away" : "team-name-home";
  const pitcherClass = isAway ? "pitcher-name-away" : "pitcher-name-home";
  const pitcherMuted = pitcherName === "尚未公布" ? " muted" : ` ${pitcherClass}`;

  return `
    <article class="team-column ${isAway ? "away-column" : "home-column"}">
      <div class="column-head">
        <span class="role-badge ${isAway ? "away-badge" : "home-badge"}">${role}</span>
        <div class="column-title-row">
          <h2 class="${teamClass}">${side.teamName}</h2>
          <span class="column-pitcher${pitcherMuted}">先發 ${pitcherName}</span>
        </div>
      </div>

      <section class="panel-block">
        <h3><span class="${teamClass}">${side.teamName}</span> 近 ${gameCount} 場 · 1–5 局得分</h3>
        ${summaryCards(side.summary, totalLabel)}
        ${teamGamesTable(side.games)}
      </section>

      <section class="panel-block">
        <h3><span class="${pitcherClass}">${pitcherDisplayName}</span> 近 ${startCount} 次先發 · 1–5 局失分（逐局掉分）</h3>
        ${
          pitcher && pitcher.games.length
            ? `${pitcherSummaryCards(pitcher.summary, pitcher.games)}${pitcherGamesTable(pitcher.games)}`
            : `<p class="empty-note">${pitcherName === "尚未公布" ? "MLB 尚未公布先發投手" : "找不到此投手近期先發數據"}</p>`
        }
      </section>
    </article>
  `;
}

function renderMatchup(data, { skipIfUnchanged = false } = {}) {
  updateCacheStatus(data);

  const fingerprint = buildContentFingerprint(data);
  if (skipIfUnchanged && fingerprint === lastContentFingerprint) {
    return false;
  }

  const tableScroll = captureTableScroll();
  lastContentFingerprint = fingerprint;

  const { matchup, away, home } = data;

  document.getElementById("matchup-title").innerHTML =
    `<span class="team-name-away">${away.teamName}</span>` +
    `<span class="at-symbol">@</span>` +
    `<span class="team-name-home">${home.teamName}</span>`;
  const taiwanTime = formatGameTime(matchup.gameDate);
  const metaParts = [matchup.date];
  if (taiwanTime) metaParts.push(`台灣 ${taiwanTime}`);
  metaParts.push(matchup.status || "Scheduled");
  document.getElementById("matchup-meta").textContent = metaParts.join(" · ");

  matchupGridEl.innerHTML = `
    ${renderSideColumn(away, "客隊")}
    ${renderSideColumn(home, "主隊")}
  `;

  const aTableRoot = document.getElementById("a-table-root");
  if (aTableRoot) {
    showATableFromMatchup(data.aTable);
  }

  restoreTableScroll(tableScroll);
  return true;
}

async function loadTeams() {
  const resp = await fetch("/api/teams");
  if (!resp.ok) throw new Error("無法載入球隊列表");
  const teams = await resp.json();

  teamSelect.innerHTML = teams
    .map((team) => `<option value="${team.id}">${team.nameZh} (${team.abbreviation})</option>`)
    .join("");

  const savedTeam = localStorage.getItem(STORAGE_KEY);
  teamSelect.value = savedTeam || String(PHILADELPHIA_ID);
}

function isDataReady(data) {
  if (data.loading) return false;
  return (data.away?.games?.length ?? 0) > 0 && (data.home?.games?.length ?? 0) > 0;
}

function setBusy(isBusy, message) {
  showLoading(isBusy);
  teamSelect.disabled = isBusy;
  refreshBtn.disabled = isBusy;
  if (message) cacheStatusEl.textContent = message;
}

function clearPollTimer() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function clearHourlyTimer() {
  if (hourlyTimer) {
    clearInterval(hourlyTimer);
    hourlyTimer = null;
  }
}

function fetchWithTimeout(url) {
  if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
    return fetch(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  return fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
}

function schedulePoll(slow = false) {
  clearPollTimer();
  pollTimer = setTimeout(
    () => fetchAnalysis(false, true, true),
    slow ? REFRESH_POLL_MS : POLL_MS
  );
}

function needsFreshData(data, games) {
  if (isDataReady(data)) {
    return false;
  }
  if (!data.cacheVersion || data.cacheVersion < expectedCacheVersion) {
    return true;
  }
  const today = new Date().toISOString().slice(0, 10);
  for (const side of ["away", "home"]) {
    for (const game of data[side]?.games ?? []) {
      if (game.date > today) return true;
      if (game.result == null && game.firstFiveRuns === 0 && game.opponentStarter == null) {
        return true;
      }
    }
  }
  return false;
}

async function loadMeta() {
  try {
    const resp = await fetch("/api/meta");
    if (resp.ok) {
      const meta = await resp.json();
      if (meta.mlbCacheVersion) expectedCacheVersion = meta.mlbCacheVersion;
    }
  } catch (_) {
    /* server offline */
  }
}

async function fetchAnalysis(force = false, allowAutoRetry = true, isPoll = false) {
  const teamId = teamSelect.value;
  const games = Number(gameCountInput.value) || 10;
  if (!teamId) return;
  if (fetchInFlight && isPoll) return;

  localStorage.setItem(STORAGE_KEY, teamId);
  showError("");
  fetchInFlight = true;
  const token = ++fetchToken;

  let ready = false;
  if (!isPoll) {
    pollAttempts = 0;
    clearPollTimer();
    if (!hasDisplayedData) {
      setBusy(true, force ? "正在更新資料…" : "載入中，請稍候…");
    } else {
      cacheStatusEl.textContent = "切換球隊中…";
    }
    resultsEl.classList.remove("hidden");
  }

  try {
    const query = new URLSearchParams({ team_id: teamId, games: String(games) });
    if (force) query.set("force", "true");

    const resp = await fetchWithTimeout(`/api/matchup?${query}`);
    const data = await resp.json();
    if (token !== fetchToken) return;
    if (!resp.ok) throw new Error(data.detail || "載入失敗");

    if (!force && allowAutoRetry && needsFreshData(data, games)) {
      cacheStatusEl.textContent = "偵測到舊資料，正在自動更新…";
      fetchInFlight = false;
      setBusy(false);
      return fetchAnalysis(true, false, isPoll);
    }

    renderMatchup(data, { skipIfUnchanged: isPoll });
    hasDisplayedData = true;
    if (!aTableReady(data.aTable)) {
      loadATable("/api/mlb", teamId, { fetchWithTimeout });
    }
    ready = isDataReady(data);

    if (!ready) {
      pollAttempts += 1;
      if (pollAttempts >= MAX_POLL_ATTEMPTS) {
        ready = true;
        showError("資料載入逾時，請按「立即更新」重試。");
        clearPollTimer();
        return;
      }
      const waitMsg = hasDisplayedData
        ? `正在載入此隊資料…（${pollAttempts}/${MAX_POLL_ATTEMPTS}）`
        : `正在抓取本隊資料…（${pollAttempts}/${MAX_POLL_ATTEMPTS}，約 1～2 分鐘）`;
      cacheStatusEl.textContent = waitMsg;
      schedulePoll(false);
      return;
    }

    pollAttempts = 0;
    clearPollTimer();
    if (data.refreshing) {
      cacheStatusEl.textContent = "已顯示快取資料，背景更新中…";
      schedulePoll(true);
    }
  } catch (err) {
    clearPollTimer();
    ready = true;
    if (err.name === "AbortError" || err.name === "TimeoutError") {
      showError("連線逾時，請按「立即更新」重試。");
    } else if (err.message === "Failed to fetch" || err.name === "TypeError") {
      showError("無法連線，請稍後再試或重新整理頁面。");
    } else {
      showError(err.message || "發生未知錯誤");
    }
  } finally {
    if (token === fetchToken) {
      fetchInFlight = false;
      setBusy(false);
    }
  }
}

function scheduleHourlyRefresh() {
  clearHourlyTimer();
  hourlyTimer = setInterval(() => fetchAnalysis(false, true, false), REFRESH_MS);
}

refreshBtn.addEventListener("click", () => fetchAnalysis(true));
teamSelect.addEventListener("change", () => fetchAnalysis(false));
gameCountInput.addEventListener("change", () => fetchAnalysis(false));

window.addEventListener("pagehide", () => {
  clearPollTimer();
  clearHourlyTimer();
});

window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  fetchInFlight = false;
  clearPollTimer();
  setBusy(false);
  fetchAnalysis(false, true, false);
});

loadTeams()
  .then(() => loadMeta())
  .then(() => fetchAnalysis(false))
  .then(() => scheduleHourlyRefresh())
  .catch((err) => showError(err.message));
