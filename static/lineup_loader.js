/**
 * Shared lineup fetch/display for MLB, NPB, CPBL pages.
 * Avoids wiping visible lineups when matchup poll returns empty startingLineups.
 */
window.LineupLoader = (function () {
  const LINEUP_FETCH_TIMEOUT_MS = 120000;
  const LINEUP_POLL_MS = 4000;
  const MAX_LINEUP_POLLS = 45;

  let lineupPollTimer = null;
  let lineupPollAttempts = 0;
  let lastGoodLineups = null;

  function lineupsReady(lineups) {
    const away = lineups?.away?.batters?.length ?? 0;
    const home = lineups?.home?.batters?.length ?? 0;
    return away > 0 || home > 0;
  }

  function clearLineupPollTimer() {
    if (lineupPollTimer) {
      clearTimeout(lineupPollTimer);
      lineupPollTimer = null;
    }
  }

  function showLineupLoading(message = "打線載入中…（約 30～60 秒）") {
    const root = document.getElementById("lineup-root");
    if (!root) return;
    root.innerHTML = `
      <details class="lineup-section card" open>
        <summary class="lineup-summary">先發打線 · 本季成績</summary>
        <p class="lineup-note">${message}</p>
      </details>
    `;
  }

  function clearDisplayedLineups() {
    clearLineupPollTimer();
    lastGoodLineups = null;
    const root = document.getElementById("lineup-root");
    if (root) root.innerHTML = "";
  }

  async function fetchLineupsWhenReady({ apiPath, teamId, games, fetchWithTimeout, force = false }) {
    clearLineupPollTimer();
    lineupPollAttempts = 0;

    const poll = async () => {
      try {
        const qs = new URLSearchParams({ team_id: teamId, games: String(games) });
        if (force) qs.set("force", "true");
        const resp = await fetchWithTimeout(`${apiPath}/lineup?${qs}`, LINEUP_FETCH_TIMEOUT_MS);
        const lineups = window.ApiUtils
          ? await ApiUtils.readJson(resp)
          : await resp.json();
        if (resp.ok && lineupsReady(lineups)) {
          lastGoodLineups = lineups;
          syncLineup(lineups);
          return;
        }
      } catch (_) {
        /* retry */
      }

      lineupPollAttempts += 1;
      if (lineupPollAttempts < MAX_LINEUP_POLLS) {
        // Only force on the first attempt; retries use rebuilt cache.
        force = false;
        lineupPollTimer = setTimeout(poll, LINEUP_POLL_MS);
      } else {
        showLineupLoading("打線載入失敗，請按「立即更新」重試。");
      }
    };

    poll();
  }

  function ensureLineups(lineups, { apiPath, teamId, games, fetchWithTimeout, force = false } = {}) {
    if (!force && lineupsReady(lineups)) {
      lastGoodLineups = lineups;
      syncLineup(lineups);
      clearLineupPollTimer();
      return;
    }

    if (!force && lineupsReady(lastGoodLineups)) {
      syncLineup(lastGoodLineups);
    } else {
      showLineupLoading(force ? "正在重抓先發打線…" : "打線載入中…（約 30～60 秒）");
    }

    if (apiPath && teamId && typeof fetchWithTimeout === "function" && !window.SiteConfig?.isStatic) {
      fetchLineupsWhenReady({ apiPath, teamId, games, fetchWithTimeout, force });
    }
  }

  return {
    ensureLineups,
    clearDisplayedLineups,
    lineupsReady,
    showLineupLoading,
  };
})();
