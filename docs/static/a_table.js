function aTableCell(count, total) {
  if (!total) return "0(0%)";
  return `${count}(${Math.round((count / total) * 100)}%)`;
}

function sideATableReady(side) {
  if (!side?.recent5 || !side?.recent10 || !side?.recent20) return false;
  return (side.recent10.gameCount ?? 0) > 0;
}

function aTableReady(data) {
  return sideATableReady(data?.away) && sideATableReady(data?.home);
}

function renderATableBlock(side, roleLabel) {
  const total5 = side.recent5.gameCount || 5;

  const rows = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    .map((inning) => {
      const s5 = side.recent5.scoredCounts[String(inning)] ?? 0;
      const a5 = side.recent5.allowedCounts[String(inning)] ?? 0;
      const s10 = side.recent10.scoredCounts[String(inning)] ?? 0;
      const s20 = side.recent20.scoredCounts[String(inning)] ?? 0;
      const a10 = side.recent10.allowedCounts[String(inning)] ?? 0;
      const a20 = side.recent20.allowedCounts[String(inning)] ?? 0;
      return `
        <tr>
          <td class="col-inning">${inning}</td>
          <td>${aTableCell(s5, total5)}</td>
          <td>${aTableCell(s10, 10)}</td>
          <td>${aTableCell(s20, 20)}</td>
          <td class="col-allowed-start">${aTableCell(a5, total5)}</td>
          <td>${aTableCell(a10, 10)}</td>
          <td>${aTableCell(a20, 20)}</td>
        </tr>
      `;
    })
    .join("");

  return `
    <article class="a-table-block">
      <h4 class="a-table-team">${roleLabel} · ${side.teamName}</h4>
      <div class="table-wrap">
        <table class="data-table a-table">
          <thead>
            <tr>
              <th class="col-inning">局</th>
              <th>5得</th>
              <th>10得</th>
              <th>20得</th>
              <th class="col-allowed-start">5失</th>
              <th>10失</th>
              <th>20失</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </article>
  `;
}

function renderATableSection(aTable, { loading = false, error = "" } = {}) {
  const loadingText = "a表格 載入中…";
  const awayBlock = sideATableReady(aTable?.away)
    ? renderATableBlock(aTable.away, "客隊")
    : `<p class="empty-note">${error || (loading ? loadingText : "尚無 a表格 數據")}</p>`;
  const homeBlock = sideATableReady(aTable?.home)
    ? renderATableBlock(aTable.home, "主隊")
    : loading && !error
      ? `<p class="empty-note">${loadingText}</p>`
      : "";

  return `
    <details class="a-table-section card" open>
      <summary class="a-table-summary">近5 / 近10 / 近20 · 各局得失场数（a表格）</summary>
      <p class="a-table-note">5得 / 10得 / 20得 = 近5/10/20場該局有得分場數；5失 / 10失 / 20失 = 近5/10/20場該局有失分場數。括號為比例。</p>
      <div class="a-table-grid">
        ${awayBlock}
        ${homeBlock}
      </div>
    </details>
  `;
}

const ATABLE_FETCH_MS = 300000;

let aTableLoadToken = 0;
let aTableCachedTeamId = null;
let aTableLoadInProgress = false;
let aTableLoadTeamId = null;

function cancelATableLoad() {
  aTableLoadToken += 1;
  aTableLoadInProgress = false;
  aTableLoadTeamId = null;
  window.__lastATable = null;
  aTableCachedTeamId = null;
}

function normalizeTeamId(teamId) {
  return teamId == null ? "" : String(teamId);
}

function hasCachedATableForTeam(teamId) {
  const tid = normalizeTeamId(teamId);
  return aTableReady(window.__lastATable) && aTableCachedTeamId === tid;
}

function paintATable(aTable, teamId) {
  const root = document.getElementById("a-table-root");
  if (!root) return;
  const tid = normalizeTeamId(teamId);
  window.__lastATable = aTable;
  aTableCachedTeamId = tid;
  root.innerHTML = renderATableSection(aTable);
}

function showATableFromMatchup(aTable, teamId, { matchReady = false } = {}) {
  const root = document.getElementById("a-table-root");
  if (!root) return;

  const tid = normalizeTeamId(teamId);

  if (aTableReady(aTable)) {
    paintATable(aTable, tid);
    return;
  }

  if (hasCachedATableForTeam(tid)) {
    root.innerHTML = renderATableSection(window.__lastATable);
    return;
  }

  if (aTableLoadInProgress && aTableLoadTeamId === tid) {
    return;
  }

  if (matchReady) {
    root.innerHTML = renderATableSection(null, { loading: true });
  }
}

async function loadATable(apiBase, teamId, { force = false } = {}) {
  const root = document.getElementById("a-table-root");
  const tid = normalizeTeamId(teamId);
  if (!root || !tid) return;

  if (hasCachedATableForTeam(tid)) {
    root.innerHTML = renderATableSection(window.__lastATable);
    return;
  }

  if (aTableLoadInProgress && aTableLoadTeamId === tid) {
    return;
  }

  const token = ++aTableLoadToken;
  aTableLoadInProgress = true;
  aTableLoadTeamId = tid;
  root.innerHTML = renderATableSection(null, { loading: true });

  const qs = new URLSearchParams({ team_id: tid });
  if (force) qs.set("force", "true");
  const url = `${apiBase}/a-table?${qs}`;

  try {
    const resp =
      typeof AbortSignal !== "undefined" && AbortSignal.timeout
        ? await fetch(url, { signal: AbortSignal.timeout(ATABLE_FETCH_MS) })
        : await fetch(url);
    const data = await resp.json();
    if (token !== aTableLoadToken) return;

    if (!resp.ok) {
      root.innerHTML = renderATableSection(null, { error: data.detail || "a表格 載入失敗" });
      return;
    }

    if (aTableReady(data)) {
      paintATable(data, tid);
      return;
    }

    root.innerHTML = renderATableSection(null, { error: "a表格 資料不完整，請按「立即更新」" });
  } catch (err) {
    if (token !== aTableLoadToken) return;
    const msg =
      err.name === "AbortError" || err.name === "TimeoutError"
        ? "a表格 計算逾時，請按「立即更新」"
        : "a表格 連線失敗";
    root.innerHTML = renderATableSection(null, { error: msg });
  } finally {
    if (token === aTableLoadToken) {
      aTableLoadInProgress = false;
      if (aTableLoadTeamId === tid) {
        aTableLoadTeamId = null;
      }
    }
  }
}

function syncATable(apiBase, teamId, aTable, { force = false, matchReady = false } = {}) {
  const tid = normalizeTeamId(teamId);
  showATableFromMatchup(aTable, tid, { matchReady });
  if (matchReady && !aTableReady(aTable) && !hasCachedATableForTeam(tid)) {
    loadATable(apiBase, tid, { force });
  }
}
