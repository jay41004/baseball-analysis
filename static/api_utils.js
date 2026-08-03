/**
 * Local / Render = live API. GitHub Pages = static JSON (refreshed by Actions).
 */
window.SiteConfig = (function () {
  const isGhPages = location.hostname.includes("github.io");
  // Static dump is the independent phone path — no Render free-tier dependency.
  const isStatic = isGhPages || window.SITE_STATIC === true;
  const useLiveApi = !isStatic && window.SITE_LIVE === true;
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
