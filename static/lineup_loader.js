/**
 * Shared lineup fetch/display for MLB, NPB, CPBL pages.
 */
window.LineupLoader = (function () {
  const LINEUP_FETCH_TIMEOUT_MS = 120000;
  const LINEUP_POLL_MS = 4000;
  const MAX_LINEUP_POLLS = 45;
  const MIN_LINEUP_BATTERS = 7;

  let lineupPollTimer = null;
  let lineupPollAttempts = 0;
  let lastGoodLineups = null;
  let lastLineupKey = null;
  let lineupFetchGeneration = 0;

  function lineupMatchesContext(lineups, context) {
    if (!lineups || !context) return false;
    const gameDate = String(context.gameDate || "").slice(0, 10);
    if (!gameDate) return true;
    for (const side of ["away", "home"]) {
      const sideData = lineups[side] || {};
      const count = sideData.batters?.length ?? 0;
      if (!count) continue;
      const sourceDate = String(sideData.sourceDate || "").slice(0, 10);
      const source = String(sideData.source || "").trim().toLowerCase();
      if (sourceDate && sourceDate !== gameDate) return false;
      if (source && source !== "confirmed" && source !== "pending" && sourceDate !== gameDate) {
        return false;
      }
    }
    return true;
  }

  function buildLineupContext({ league, teamId, games, matchup, pick }) {
    return {
      league,
      teamId: String(teamId || ""),
      games: String(games || ""),
      gameDate: String(matchup?.date || pick?.date || "").slice(0, 10),
      awayTeamId: String(pick?.awayTeamId || ""),
      homeTeamId: String(pick?.homeTeamId || ""),
    };
  }

  function lineupKey(league, teamId, games) {
    return `${league || ""}:${teamId || ""}:${games || ""}`;
  }

  function sideCount(lineups, side) {
    return lineups?.[side]?.batters?.length ?? 0;
  }

  function lineupsReady(lineups) {
    return (
      sideCount(lineups, "away") >= MIN_LINEUP_BATTERS &&
      sideCount(lineups, "home") >= MIN_LINEUP_BATTERS
    );
  }

  function lineupsPartial(lineups) {
    if (!lineups || lineupsReady(lineups)) return false;
    return (
      sideCount(lineups, "away") >= MIN_LINEUP_BATTERS ||
      sideCount(lineups, "home") >= MIN_LINEUP_BATTERS
    );
  }

  function lineupsPending(lineups) {
    if (!lineups || lineupsReady(lineups) || lineupsPartial(lineups)) return false;
    const sides = [lineups.away, lineups.home].filter(Boolean);
    if (!sides.length) return true;
    return sides.every((side) => {
      const source = String(side.source || "").toLowerCase();
      return source === "pending" || source === "";
    });
  }

  function showLineupPending(attempt = 0) {
    const suffix =
      attempt > 0
        ? `（${attempt}/${MAX_LINEUP_POLLS}，持續檢查中…）`
        : "（持續檢查中…）";
    showLineupLoading(`先發打線尚未公布${suffix}`);
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

  async function fetchLineupsWhenReady({
    apiPath,
    teamId,
    games,
    fetchWithTimeout,
    force = false,
    pick = null,
    context = null,
  }) {
    clearLineupPollTimer();
    lineupPollAttempts = 0;
    let firstForce = Boolean(force);
    const generation = ++lineupFetchGeneration;
    const activeContext = context || buildLineupContext({ teamId, games, pick });

    const poll = async () => {
      if (generation !== lineupFetchGeneration) return;
      try {
        const qs = new URLSearchParams({ team_id: teamId, games: String(games) });
        if (firstForce) qs.set("force", "true");
        if (window.SiteConfig?.appendPickQuery) {
          SiteConfig.appendPickQuery(qs, pick);
        } else if (pick?.date) {
          qs.set("expected_date", String(pick.date).slice(0, 10));
          qs.set("expected_away", String(pick.awayTeamId));
          qs.set("expected_home", String(pick.homeTeamId));
        }
        const resp = await fetchWithTimeout(`${apiPath}/lineup?${qs}`, LINEUP_FETCH_TIMEOUT_MS);
        if (generation !== lineupFetchGeneration) return;
        const lineups = window.ApiUtils
          ? await ApiUtils.readJson(resp)
          : await resp.json();
        if (resp.ok && lineupsReady(lineups) && lineupMatchesContext(lineups, activeContext)) {
          lastGoodLineups = lineups;
          syncLineup(lineups);
          return;
        }
        if (resp.ok && lineupsPartial(lineups) && lineupMatchesContext(lineups, activeContext)) {
          lastGoodLineups = lineups;
          syncLineup(lineups);
          showLineupLoading(
            `已載入部分打線，等待另一隊先發公布…（${lineupPollAttempts + 1}/${MAX_LINEUP_POLLS}）`
          );
        } else if (resp.ok && lineupsPending(lineups)) {
          showLineupPending(lineupPollAttempts + 1);
        }
      } catch (_) {
        /* retry */
      }

      firstForce = false;
      lineupPollAttempts += 1;
      if (generation !== lineupFetchGeneration) return;
      if (lineupPollAttempts < MAX_LINEUP_POLLS) {
        lineupPollTimer = setTimeout(poll, LINEUP_POLL_MS);
      } else if (
        lineupsPartial(lastGoodLineups) &&
        lineupMatchesContext(lastGoodLineups, activeContext)
      ) {
        syncLineup(lastGoodLineups);
        showLineupLoading("另一隊先發尚未公布，請稍後再按「立即更新」。");
      } else {
        showLineupLoading("打線載入失敗，請按「立即更新」重試。");
      }
    };

    poll();
  }

  function ensureLineups(
    lineups,
    { apiPath, league, teamId, games, fetchWithTimeout, force = false, matchup = null, pick = null } = {}
  ) {
    const key = lineupKey(league, teamId, games);
    if (key !== lastLineupKey) {
      lastLineupKey = key;
      lastGoodLineups = null;
      clearLineupPollTimer();
    }

    const resolvedPick =
      pick ||
      (window.ApiUtils?.matchupPick && league ? ApiUtils.matchupPick.load(league) : null);
    const context = buildLineupContext({
      league,
      teamId,
      games,
      matchup,
      pick: resolvedPick,
    });

    const cfg = window.SiteConfig || {};
    const snapshotOk =
      lineupsReady(lineups) && lineupMatchesContext(lineups, context);
    const needsLive =
      !snapshotOk &&
      (force ||
        (cfg.lineupsNeedLiveRefresh && cfg.lineupsNeedLiveRefresh(lineups, matchup)));
    const useLiveOnStatic =
      cfg.isStatic && league && needsLive && typeof fetchWithTimeout === "function";
    const effectiveApiPath = useLiveOnStatic
      ? cfg.liveLineupApi(league)
      : cfg.isStatic
        ? null
        : apiPath;

    if (snapshotOk) {
      lastGoodLineups = lineups;
      syncLineup(lineups);
      if (!force && !needsLive) {
        clearLineupPollTimer();
        return;
      }
    } else if (lineupsReady(lastGoodLineups) && lineupMatchesContext(lastGoodLineups, context)) {
      syncLineup(lastGoodLineups);
    } else if (lineupsPartial(lineups)) {
      lastGoodLineups = lineups;
      syncLineup(lineups);
      showLineupLoading("已載入部分打線，等待另一隊先發公布…");
    } else if (lineupsPartial(lastGoodLineups)) {
      syncLineup(lastGoodLineups);
      showLineupLoading("已載入部分打線，等待另一隊先發公布…");
    } else if (lineupsPending(lineups)) {
      showLineupPending();
    } else {
      showLineupLoading(
        useLiveOnStatic
          ? "正在向雲端抓取最新先發打線…（約 30～90 秒）"
          : force
            ? "正在重抓先發打線…"
            : "打線載入中…（約 30～60 秒）"
      );
    }

    if (effectiveApiPath && teamId && typeof fetchWithTimeout === "function" && needsLive) {
      fetchLineupsWhenReady({
        apiPath: effectiveApiPath,
        teamId,
        games,
        fetchWithTimeout,
        force: Boolean(force),
        pick: resolvedPick,
        context,
      });
    }
  }

  return {
    ensureLineups,
    clearDisplayedLineups,
    lineupsReady,
    showLineupLoading,
  };
})();
