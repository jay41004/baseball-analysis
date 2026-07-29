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
    const retries = options.retries ?? 12;
    const retryMs = options.retryMs ?? 5000;
    const onWaiting = options.onWaiting;

    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        const resp = await fetchFn(url);
        if (WAKE_STATUSES.has(resp.status) && attempt < retries) {
          if (onWaiting) {
            onWaiting(attempt + 1, retries + 1);
          }
          await new Promise((resolve) => setTimeout(resolve, retryMs));
          continue;
        }
        const data = await readJson(resp);
        return { resp, data };
      } catch (err) {
        if (err.message === "WAKE_UP" && attempt < retries) {
          if (onWaiting) {
            onWaiting(attempt + 1, retries + 1);
          }
          await new Promise((resolve) => setTimeout(resolve, retryMs));
          continue;
        }
        throw err;
      }
    }
    throw new Error("雲端伺服器啟動中，請 30 秒後重新整理");
  }

  return { readJson, fetchJson, isHtmlBody };
})();
