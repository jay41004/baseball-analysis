/**
 * Today / tomorrow two-column game picker (PlaySport-style).
 * Click a game → set hidden team select → load analysis below.
 */
window.GamePicker = (function () {
  const WEEK = ["日", "一", "二", "三", "四", "五", "六"];

  function dayLabel(iso) {
    const d = new Date(`${iso}T12:00:00+08:00`);
    return `${d.getMonth() + 1}月${d.getDate()} ${WEEK[d.getDay()]}`;
  }

  function formatTimeDisplay(hhmm) {
    if (!hhmm || !String(hhmm).includes(":")) return "—";
    return String(hhmm);
  }

  function shortTeamName(name) {
    const s = String(name || "").trim();
    if (!s) return "—";
    const stripped = s
      .replace(/^(東京|福岡|北海道|東北|千葉|廣島|讀賣|阪神|統一|中信|味全|富邦|台鋼)/u, "")
      .replace(/(龍|虎|鯉|鷹|獅|猿|熊|牛|悍將|兄弟|雄鷹|桃猿|DeNA)$/iu, "")
      .trim();
    return stripped || s;
  }

  function gameKey(row) {
    return `${row.date}|${row.awayTeamId}|${row.homeTeamId}`;
  }

  function isSelected(row, teamId) {
    const id = String(teamId || "");
    if (!id) return false;
    return String(row.awayTeamId) === id || String(row.homeTeamId) === id;
  }

  function findSelectedKey(games, teamId) {
    for (const row of games) {
      if (isSelected(row, teamId)) return gameKey(row);
    }
    return null;
  }

  function renderGame(row, selectedKey) {
    const key = gameKey(row);
    const active = key === selectedKey ? " active" : "";
    const away = shortTeamName(row.awayName);
    const home = shortTeamName(row.homeName);
    const time = formatTimeDisplay(row.timeTaiwan);
    return `
      <button type="button" class="game-pick-item${active}" data-team-id="${row.awayTeamId}" data-game-key="${key}">
        <span class="game-pick-time">${time || "—"}</span>
        <span class="game-pick-teams">${away} <span class="game-pick-vs">VS</span> ${home}</span>
      </button>
    `;
  }

  function renderColumn(dateIso, games, selectedKey) {
    const items = (games || []).map((row) => renderGame(row, selectedKey)).join("");
    return `
      <div class="game-picker-col">
        <h3 class="game-picker-date">${dayLabel(dateIso)}</h3>
        <div class="game-picker-list">
          ${items || '<p class="game-picker-empty">本日無賽事</p>'}
        </div>
      </div>
    `;
  }

  function setActive(root, teamId, games) {
    const selectedKey = findSelectedKey(games, teamId);
    root.querySelectorAll(".game-pick-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.gameKey === selectedKey);
    });
  }

  function showTeamSelectFallback(teamSelect) {
    if (!teamSelect) return;
    teamSelect.classList.remove("visually-hidden");
    const label = teamSelect.closest(".controls")?.querySelector("label[for='team-select']");
    if (label) label.classList.remove("visually-hidden");
  }

  function bindClicks(root, teamSelect, onSelect, allGames) {
    root.querySelectorAll(".game-pick-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const teamId = btn.dataset.teamId;
        if (!teamId || teamSelect.value === teamId) return;
        teamSelect.value = teamId;
        setActive(root, teamId, allGames);
        if (onSelect) onSelect(teamId);
      });
    });
  }

  async function mount({ league, teamSelect, onSelect, root }) {
    const el = root || document.getElementById("game-picker-root");
    if (!el || !teamSelect) return;

    el.innerHTML = '<p class="game-picker-loading">載入賽程…</p>';

    const url =
      typeof SiteConfig.slate === "function"
        ? SiteConfig.slate(league)
        : `/api/slate?league=${encodeURIComponent(league)}`;
    try {
      const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), 25000) : null;
      const fetchFn = (u) =>
        fetch(u, controller ? { signal: controller.signal } : undefined).finally(() => {
          if (timer) clearTimeout(timer);
        });
      const { resp, data } = await ApiUtils.fetchJson(url, fetchFn, {
        retries: SiteConfig.isStatic ? 1 : 3,
        retryMs: SiteConfig.isStatic ? 500 : 2000,
      });
      if (!resp.ok) throw new Error(data.detail || "賽程載入失敗");

      const leagueData = data[league] || {};
      const todayGames = data.todayGames || leagueData.today || [];
      const tomorrowGames = data.tomorrowGames || leagueData.tomorrow || [];
      const allGames = [...todayGames, ...tomorrowGames];
      const selectedKey = findSelectedKey(allGames, teamSelect.value);

      el.innerHTML = `
        <div class="game-picker">
          ${renderColumn(data.today, todayGames, selectedKey)}
          ${renderColumn(data.tomorrow, tomorrowGames, selectedKey)}
        </div>
      `;

      bindClicks(el, teamSelect, onSelect, allGames);
    } catch (err) {
      el.innerHTML = `<p class="game-picker-note">賽程暫時無法載入，請用下方選單選隊。</p>`;
      showTeamSelectFallback(teamSelect);
    }
  }

  return { mount, setActive };
})();
