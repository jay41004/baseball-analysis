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
    mlbMatchup(teamId, games, force) {
      if (isStatic) return dataUrl("mlb", `matchup_${teamId}_${games}.json`);
      const q = new URLSearchParams({ team_id: teamId, games: String(games) });
      if (force) q.set("force", "true");
      return api(`/api/matchup?${q}`);
    },
    npbTeams() {
      return isStatic ? dataUrl("npb", "teams.json") : api("/api/npb/teams");
    },
    npbMatchup(teamId, games, force) {
      if (isStatic) return dataUrl("npb", `matchup_${teamId}_${games}.json`);
      const q = new URLSearchParams({ team_id: teamId, games: String(games) });
      if (force) q.set("force", "true");
      return api(`/api/npb/matchup?${q}`);
    },
    cpblTeams() {
      return isStatic ? dataUrl("cpbl", "teams.json") : api("/api/cpbl/teams");
    },
    cpblMatchup(teamId, games, force) {
      if (isStatic) return dataUrl("cpbl", `matchup_${teamId}_${games}.json`);
      const q = new URLSearchParams({ team_id: teamId, games: String(games) });
      if (force) q.set("force", "true");
      return api(`/api/cpbl/matchup?${q}`);
    },
    meta() {
      return isStatic ? `${base}/data/meta.json` : api("/api/meta");
    },
    /** Render live API for lineup-only refresh on static GitHub Pages. */
    liveLineupApi(league) {
      return `${liveApiRoot}/api/${league}`;
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
    const retries = SiteConfig.isStatic ? 2 : options.retries ?? 12;
    const retryMs = options.retryMs ?? 5000;
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

  return { readJson, fetchJson, isHtmlBody };
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
