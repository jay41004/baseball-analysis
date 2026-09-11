/**
 * Local / Render = live API. GitHub Pages = static JSON (refreshed by Actions).
 */
window.SiteConfig = (function () {
  const isGhPages = location.hostname.includes("github.io");
  // Static dump is the independent phone path — no Render free-tier dependency.
  const isStatic = isGhPages || window.SITE_STATIC === true;
  const useLiveApi = !isStatic && window.SITE_LIVE === true;
  const liveApiRoot = "https://baseball-analysis.onrender.com";
  const parts = location.pathname.split("/").filter(Boolean);
  const repo = isGhPages && parts.length ? parts[0] : "";
  const base = repo ? `/${repo}` : "";
  const apiRoot = "";

  function dataUrl(league, file) {
    return `${base}/data/${league}/${file}`;
  }

  function api(path) {
    if (path.startsWith("http")) return path;
    return `${apiRoot}${path}`;
  }

  function appendPickQuery(q, pick) {
    if (!pick) return;
    if (pick.gamePk) q.set("expected_game_pk", String(pick.gamePk));
    if (!pick?.date) return;
    q.set("expected_date", String(pick.date).slice(0, 10));
    q.set("expected_away", String(pick.awayTeamId));
    q.set("expected_home", String(pick.homeTeamId));
  }

  return {
    isStatic,
    isGhPages,
    useLiveApi,
    liveApiRoot,
    apiRoot,
    base,
    dataUrl,
    api,
    mlbTeams() {
      return isStatic ? dataUrl("mlb", "teams.json") : api("/api/teams");
    },
    mlbMatchup(teamId, games, force, pick) {
      if (isStatic) return dataUrl("mlb", `matchup_${teamId}_${games}.json`);
      const q = new URLSearchParams({ team_id: teamId, games: String(games) });
      if (force) q.set("force", "true");
      appendPickQuery(q, pick);
      return api(`/api/matchup?${q}`);
    },
    npbTeams() {
      return isStatic ? dataUrl("npb", "teams.json") : api("/api/npb/teams");
    },
    npbMatchup(teamId, games, force, pick) {
      if (isStatic) return dataUrl("npb", `matchup_${teamId}_${games}.json`);
      const q = new URLSearchParams({ team_id: teamId, games: String(games) });
      if (force) q.set("force", "true");
      appendPickQuery(q, pick);
      return api(`/api/npb/matchup?${q}`);
    },
    npbMatchupLive(teamId, games, force, pick) {
      const q = new URLSearchParams({ team_id: teamId, games: String(games) });
      if (force) q.set("force", "true");
      appendPickQuery(q, pick);
      return `${liveApiRoot}/api/npb/matchup?${q}`;
    },
    cpblTeams() {
      return isStatic ? dataUrl("cpbl", "teams.json") : api("/api/cpbl/teams");
    },
    cpblMatchup(teamId, games, force, pick) {
      if (isStatic) return dataUrl("cpbl", `matchup_${teamId}_${games}.json`);
      const q = new URLSearchParams({ team_id: teamId, games: String(games) });
      if (force) q.set("force", "true");
      appendPickQuery(q, pick);
      return api(`/api/cpbl/matchup?${q}`);
    },
    meta() {
      return isStatic ? `${base}/data/meta.json` : api("/api/meta");
    },
    slate(league) {
      if (isStatic) {
        if (league) return dataUrl(league, "slate.json");
        return `${base}/data/slate.json`;
      }
      const q = league ? `?league=${encodeURIComponent(league)}` : "";
      return api(`/api/slate${q}`);
    },
    /** Render live API for lineup-only refresh on static GitHub Pages. */
    liveLineupApi(league) {
      return `${liveApiRoot}/api/${league}`;
    },
    jstTodayYmd() {
      return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Tokyo" });
    },
    /** Static Pages: upgrade matchup header (date/pitchers) from Render when stale. */
    matchupNeedsLiveRefresh(league, data, pick) {
      if (!isStatic || !data) return false;
      const matchup = data.matchup || {};
      const md = String(matchup.date || "").slice(0, 10);
      const today =
        league === "npb" ? this.jstTodayYmd() : new Date().toLocaleDateString("en-CA", {
            timeZone: "Asia/Taipei",
          });
      if (md && md < today) return true;
      if (pick?.date) {
        const pd = String(pick.date).slice(0, 10);
        if (pd && md && pd !== md) return true;
      }
      if (league === "npb" && md === today) return true;
      if (league !== "npb" && window.MatchupMeta?.isStaleMatchup?.(matchup)) return true;
      return false;
    },
    /** True when static snapshot lineups should be upgraded from Render. */
    lineupsNeedLiveRefresh(lineups, matchup) {
      if (!matchup?.date) return false;
      const gameDate = String(matchup.date).slice(0, 10);
      const status = String(matchup.status || "")
        .trim()
        .toLowerCase();
      const away = lineups?.away?.batters?.length ?? 0;
      const home = lineups?.home?.batters?.length ?? 0;
      if (!away && !home) return true;
      if (status === "final") return false;
      for (const side of ["away", "home"]) {
        const sideData = lineups?.[side] || {};
        const count = sideData.batters?.length ?? 0;
        if (!count) continue;
        const sourceDate = String(sideData.sourceDate || "").slice(0, 10);
        const source = String(sideData.source || "")
          .trim()
          .toLowerCase();
        if (sourceDate && sourceDate !== gameDate) return true;
        if (
          source !== "confirmed" &&
          sourceDate === gameDate &&
          ["scheduled", "live", "in progress", "preview", "warmup", ""].includes(status)
        ) {
          return true;
        }
      }
      return false;
    },
    appendPickQuery,
    /**
     * GitHub Pages: merge Render live header into static snapshot — never replace
     * team panels, pitcherAnalysis.games, or aTable wholesale.
     */
    mergeLiveMatchupHeader(staticData, liveData, league) {
      if (!staticData) return liveData || null;
      if (!liveData) return staticData;
      const out = JSON.parse(JSON.stringify(staticData));
      const sm = staticData.matchup || {};
      const lm = liveData.matchup || {};
      out.matchup = { ...sm, ...lm };
      for (const key of ["taiwanDate", "officialDate", "timeTaiwan", "timeLocal", "gamePk", "gameSno"]) {
        if (!out.matchup[key] && sm[key]) out.matchup[key] = sm[key];
      }

      const pitcherName = (panel) =>
        String((panel?.probablePitcher?.fullName || panel?.pitcherAnalysis?.pitcherName || "")).trim();

      for (const side of ["away", "home"]) {
        const sp = staticData[side] || {};
        const lp = liveData[side] || {};
        const panel = { ...sp };
        if (lp.teamId != null) panel.teamId = lp.teamId;
        if (lp.teamName) panel.teamName = lp.teamName;

        const oldPitcher = pitcherName(sp);
        const newPitcher = pitcherName(lp);
        if (lp.probablePitcher) {
          panel.probablePitcher = lp.probablePitcher;
        }

        const staticGames = sp.pitcherAnalysis?.games || [];
        const liveGames = lp.pitcherAnalysis?.games || [];
        if (newPitcher && oldPitcher && newPitcher !== oldPitcher) {
          panel.pitcherAnalysis = liveGames.length ? lp.pitcherAnalysis : undefined;
        } else if (staticGames.length) {
          panel.pitcherAnalysis = sp.pitcherAnalysis;
        } else if (liveGames.length) {
          panel.pitcherAnalysis = lp.pitcherAnalysis;
        }

        if (!(panel.games || []).length && (lp.games || []).length) {
          panel.games = lp.games;
        }
        if (!panel.summary && lp.summary) {
          panel.summary = lp.summary;
        }
        out[side] = panel;
      }

      const staticLineups = staticData.startingLineups;
      const liveLineups = liveData.startingLineups;
      if (
        liveLineups &&
        this.lineupsNeedLiveRefresh(staticLineups, out.matchup) &&
        ((liveLineups.away?.batters?.length ?? 0) >= 7 ||
          (liveLineups.home?.batters?.length ?? 0) >= 7)
      ) {
        out.startingLineups = liveLineups;
      } else if (staticLineups) {
        out.startingLineups = staticLineups;
      }

      if (!out.aTable && liveData.aTable) out.aTable = liveData.aTable;
      if (!out.situational && liveData.situational) out.situational = liveData.situational;
      if (staticData.cacheVersion) out.cacheVersion = staticData.cacheVersion;
      out.liveHeaderMerged = true;
      out.liveHeaderLeague = league || "";
      return out;
    },
  };
})();

/**
 * Safe JSON fetch with Render cold-start retry (502 returns HTML, not JSON).
 */
window.ApiUtils = (function () {
  const WAKE_STATUSES = new Set([502, 503, 504]);

  function isHtmlBody(text) {
    const trimmed = (text || "").trimStart();
    return trimmed.startsWith("<!") || trimmed.startsWith("<html");
  }

  async function readJson(resp) {
    const text = await resp.text();
    if (isHtmlBody(text)) {
      const err = new Error("WAKE_UP");
      err.status = resp.status;
      throw err;
    }
    try {
      return text ? JSON.parse(text) : {};
    } catch (_) {
      throw new Error("伺服器回應格式錯誤，請重新整理頁面");
    }
  }

  async function fetchJson(url, fetchFn, options = {}) {
    const retries = SiteConfig.isStatic ? 1 : options.retries ?? 8;
    const retryMs = options.retryMs ?? 3000;
    const onWaiting = options.onWaiting;

    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        const resp = await fetchFn(url);
        if (WAKE_STATUSES.has(resp.status) && attempt < retries) {
          if (onWaiting) onWaiting(attempt + 1, retries + 1);
          await new Promise((resolve) => setTimeout(resolve, retryMs));
          continue;
        }
        const data = await readJson(resp);
        return { resp, data };
      } catch (err) {
        if (err.message === "WAKE_UP" && attempt < retries) {
          if (onWaiting) onWaiting(attempt + 1, retries + 1);
          await new Promise((resolve) => setTimeout(resolve, retryMs));
          continue;
        }
        throw err;
      }
    }
    throw new Error(
      SiteConfig.isStatic
        ? "資料載入失敗，請重新整理"
        : "雲端伺服器啟動中，請 30 秒後重新整理"
    );
  }

  function matchupHasHeader(data) {
    const date = data?.matchup?.date;
    const away = data?.away?.teamName;
    const home = data?.home?.teamName;
    return Boolean(
      date &&
        date !== "—" &&
        away &&
        away !== "載入中…" &&
        home &&
        home !== "載入中…"
    );
  }

  function isMatchupDataReady(data) {
    if (!data || data.loading) return false;
    const hasGames =
      (data.away?.games?.length ?? 0) > 0 && (data.home?.games?.length ?? 0) > 0;
    if (hasGames) return true;
    return matchupHasHeader(data);
  }

  const PICK_SCHEMA_VERSION = 3;

  const matchupPick = {
    storageKey(league) {
      return `picked_game_${league}`;
    },
    save(league, row) {
      if (!row?.date && !row?.gamePk) return;
      sessionStorage.setItem(
        this.storageKey(league),
        JSON.stringify({
          v: PICK_SCHEMA_VERSION,
          date: row.date,
          awayTeamId: row.awayTeamId,
          homeTeamId: row.homeTeamId,
          gamePk: row.gamePk || null,
          taiwanDate: row.taiwanDate || null,
        })
      );
    },
    load(league) {
      try {
        const raw = sessionStorage.getItem(this.storageKey(league));
        if (!raw) return null;
        const pick = JSON.parse(raw);
        if (pick?.v !== PICK_SCHEMA_VERSION) {
          sessionStorage.removeItem(this.storageKey(league));
          return null;
        }
        return pick;
      } catch (_) {
        return null;
      }
    },
    clear(league) {
      sessionStorage.removeItem(this.storageKey(league));
    },
  };

  function matchupMatchesPick(data, pick) {
    if (!pick || !data) return true;
    const pk = pick.gamePk ? Number(pick.gamePk) : null;
    const hdrPk = data.matchup?.gamePk ? Number(data.matchup.gamePk) : null;
    if (pk && hdrPk && pk === hdrPk) return true;
    if (!pick?.date) return true;
    const md = String(data.matchup?.date || "").slice(0, 10);
    const od = String(data.matchup?.officialDate || "").slice(0, 10);
    const away = String(data.away?.teamId ?? "");
    const home = String(data.home?.teamId ?? "");
    const pa = String(pick.awayTeamId);
    const ph = String(pick.homeTeamId);
    const pd = String(pick.date).slice(0, 10);
    if (pd !== md && pd !== od) return false;
    return (away === pa && home === ph) || (away === ph && home === pa);
  }

  async function fetchMatchupForPick({
    buildUrl,
    teamId,
    games,
    force,
    pick,
    league,
    fetchFn,
    fetchJsonOpts = {},
  }) {
    const load = async (tid, f, activePick = pick) =>
      fetchJson(buildUrl(tid, games, f, activePick), fetchFn, fetchJsonOpts);
    let primary = await load(teamId, force);
    if (primary.data?.pickStale && pick && league && !force) {
      primary = await load(teamId, true, pick);
    }
    if (!pick || matchupMatchesPick(primary.data, pick)) return primary;
    const other =
      String(teamId) === String(pick.awayTeamId) ? pick.homeTeamId : pick.awayTeamId;
    const alt = await load(other, force);
    if (matchupMatchesPick(alt.data, pick)) return alt;
    if (!force && !SiteConfig.isStatic) return load(teamId, true);
    return primary;
  }

  return {
    readJson,
    fetchJson,
    isHtmlBody,
    isMatchupDataReady,
    matchupHasHeader,
    matchupPick,
    matchupMatchesPick,
    fetchMatchupForPick,
  };
})();

/** Shared matchup header helpers (today/tomorrow labels, starters). */
window.MatchupMeta = (function () {
  function taiwanTodayYmd() {
    return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Taipei" });
  }

  function taiwanTomorrowYmd() {
    const t = new Date();
    t.setTime(t.getTime() + 86_400_000);
    return t.toLocaleDateString("en-CA", { timeZone: "Asia/Taipei" });
  }

  function taiwanYmdMinusDays(ymd, days) {
    const d = new Date(`${ymd}T12:00:00+08:00`);
    d.setTime(d.getTime() - days * 86_400_000);
    return d.toLocaleDateString("en-CA", { timeZone: "Asia/Taipei" });
  }

  function dayLabel(officialDateYmd, taiwanDateYmd) {
    const official = String(officialDateYmd || "").slice(0, 10);
    const tw = String(taiwanDateYmd || "").slice(0, 10);
    const today = taiwanTodayYmd();
    const tomorrow = taiwanTomorrowYmd();
    const usForTodayCol = taiwanYmdMinusDays(today, 1);
    const usForTomorrowCol = taiwanYmdMinusDays(tomorrow, 1);
    // 台灣今天欄→美國昨天；台灣明天欄→美國今天（例：台 9/7/9/8 → 美 9/6/9/7）
    if (official === usForTodayCol) return "今日賽事";
    if (official === usForTomorrowCol) return "明日賽事";
    if (official && official < usForTodayCol) return "快照已過期";
    if (tw === today) return "今日賽事";
    if (tw === tomorrow) return "明日賽事";
    return `${official || tw} 賽事`;
  }

  function formatGameTime(iso, timeTaiwan) {
    if (timeTaiwan) return String(timeTaiwan);
    if (!iso) return "";
    const raw = String(iso).trim();
    const hasTz = /[zZ]|[+-]\d{2}:\d{2}$/.test(raw);
    const normalized = hasTz ? raw : `${raw}Z`;
    return new Date(normalized).toLocaleString("zh-TW", {
      timeZone: "Asia/Taipei",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function buildMetaText(matchup, away, home) {
    const us = String(matchup?.officialDate || "").slice(0, 10);
    const tw = String(matchup?.taiwanDate || matchup?.date || "").slice(0, 10);
    const time = matchup?.timeTaiwan || "";
    const label = dayLabel(us, tw);
    const parts = [];
    if (label) parts.push(label);
    if (us) parts.push(`美國 ${us}`);
    if (tw && time) parts.push(`台灣 ${tw} ${time} 開球`);
    else if (tw) parts.push(`台灣 ${tw}`);
    parts.push(matchup?.status || "Scheduled");
    const awayP = away?.probablePitcher?.fullName;
    const homeP = home?.probablePitcher?.fullName;
    if (awayP || homeP) {
      parts.push(`先發 ${awayP || "待定"} vs ${homeP || "待定"}`);
    } else if (us === taiwanTodayYmd()) {
      parts.push("先發：官網尚未公布");
    }
    return parts.join(" · ");
  }

  function isStaleMatchup(matchup) {
    const gameDate = String(matchup?.date || "").slice(0, 10);
    return Boolean(gameDate && gameDate < taiwanTodayYmd());
  }

  function formatPitchCount(value) {
    if (value == null || value === "") return "-";
    const n = Number(value);
    return Number.isFinite(n) ? String(n) : "-";
  }

  return { taiwanTodayYmd, dayLabel, formatGameTime, buildMetaText, isStaleMatchup, formatPitchCount };
})();

/** Keep horizontal table scrolling from bouncing back to the page. */
(function bindTableHorizontalScroll() {
  if (window.__tableWheelBound) return;
  window.__tableWheelBound = true;

  document.addEventListener(
    "wheel",
    (event) => {
      const wrap = event.target.closest?.(".table-wrap");
      if (!wrap) return;
      if (wrap.scrollWidth <= wrap.clientWidth + 1) return;

      const absX = Math.abs(event.deltaX);
      const absY = Math.abs(event.deltaY);
      let delta = 0;
      if (absX > absY && absX > 0) {
        delta = event.deltaX;
      } else if (event.shiftKey && absY > 0) {
        delta = event.deltaY;
      } else {
        return;
      }

      const maxScroll = wrap.scrollWidth - wrap.clientWidth;
      const next = Math.min(maxScroll, Math.max(0, wrap.scrollLeft + delta));
      wrap.scrollLeft = next;
      event.preventDefault();
      event.stopPropagation();
    },
    { passive: false, capture: true }
  );
})();

/**
 * Preserve .table-wrap horizontal scroll across DOM rebuilds / polls.
 * Also detects active touch scrolling so renders can defer.
 */
window.TableScroll = (function () {
  let interactingUntil = 0;
  let bound = false;
  let lastPositions = {};

  function markInteracting(ms = 2500) {
    interactingUntil = Date.now() + ms;
  }

  function isInteracting() {
    return Date.now() < interactingUntil;
  }

  function capture() {
    const map = {};
    document.querySelectorAll(".table-wrap").forEach((el, index) => {
      const key = el.dataset.scrollKey || `idx:${index}`;
      map[key] = el.scrollLeft;
    });
    lastPositions = { ...lastPositions, ...map };
    return map;
  }

  function restore(map) {
    const merged = { ...lastPositions, ...(map || {}) };
    lastPositions = merged;
    if (!Object.keys(merged).length) return;
    const apply = () => {
      document.querySelectorAll(".table-wrap").forEach((el, index) => {
        const key = el.dataset.scrollKey || `idx:${index}`;
        if (merged[key] != null) el.scrollLeft = merged[key];
      });
    };
    apply();
    requestAnimationFrame(() => {
      apply();
      requestAnimationFrame(apply);
    });
  }

  function bind() {
    if (bound) return;
    bound = true;
    const mark = () => markInteracting();
    document.addEventListener(
      "touchstart",
      (event) => {
        if (event.target.closest?.(".table-wrap")) mark();
      },
      { passive: true, capture: true }
    );
    document.addEventListener(
      "touchmove",
      (event) => {
        if (event.target.closest?.(".table-wrap")) mark();
      },
      { passive: true, capture: true }
    );
    document.addEventListener(
      "pointerdown",
      (event) => {
        if (event.target.closest?.(".table-wrap")) mark();
      },
      { passive: true, capture: true }
    );
    document.addEventListener(
      "scroll",
      (event) => {
        const wrap = event.target?.classList?.contains("table-wrap")
          ? event.target
          : event.target?.closest?.(".table-wrap");
        if (!wrap) return;
        mark();
        const key =
          wrap.dataset.scrollKey ||
          `idx:${[...document.querySelectorAll(".table-wrap")].indexOf(wrap)}`;
        lastPositions[key] = wrap.scrollLeft;
      },
      { passive: true, capture: true }
    );
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  return { capture, restore, isInteracting, markInteracting, bind };
})();
