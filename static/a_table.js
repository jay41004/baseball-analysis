function aTableCell(count, total) {
  if (!total) return "0(0%)";
  return `${count}(${Math.round((count / total) * 100)}%)`;
}

function aTableReady(data) {
  return (
    data?.away?.recent10 &&
    data?.away?.recent20 &&
    data?.home?.recent10 &&
    data?.home?.recent20
  );
}

function renderATableBlock(side, roleLabel) {
  if (!side?.recent10 || !side?.recent20) {
    return `<p class="empty-note">a表格 載入中…</p>`;
  }

  const rows = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    .map((inning) => {
      const s10 = side.recent10.scoredCounts[String(inning)] ?? 0;
      const s20 = side.recent20.scoredCounts[String(inning)] ?? 0;
      const a10 = side.recent10.allowedCounts[String(inning)] ?? 0;
      const a20 = side.recent20.allowedCounts[String(inning)] ?? 0;
      return `
        <tr>
          <td class="col-inning">${inning}</td>
          <td>${aTableCell(s10, 10)}</td>
          <td>${aTableCell(s20, 20)}</td>
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
              <th>10得</th>
              <th>20得</th>
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

function renderATableSection(aTable, { loading = false, slow = false } = {}) {
  const loadingText = slow
    ? "a表格 仍在背景載入，請稍候或按「立即更新」"
    : "a表格 載入中…";
  const awayBlock =
    aTable?.away?.recent10 && aTable?.away?.recent20
      ? renderATableBlock(aTable.away, "客隊")
      : `<p class="empty-note">${loading ? loadingText : "尚無 a表格 數據"}</p>`;
  const homeBlock =
    aTable?.home?.recent10 && aTable?.home?.recent20
      ? renderATableBlock(aTable.home, "主隊")
      : loading
        ? `<p class="empty-note">${loadingText}</p>`
        : "";

  return `
    <details class="a-table-section card" open>
      <summary class="a-table-summary">近10 vs 近20 · 各局得失场数（a表格）</summary>
      <p class="a-table-note">10得 / 20得 = 該局有得分場數；10失 / 20失 = 該局有失分場數。括號為比例。</p>
      <div class="a-table-grid">
        ${awayBlock}
        ${homeBlock}
      </div>
    </details>
  `;
}

let aTableLoadToken = 0;
const MAX_ATABLE_ATTEMPTS = 40;
const ATABLE_POLL_MS = 3000;

async function loadATable(apiBase, teamId, { fetchWithTimeout } = {}) {
  const root = document.getElementById("a-table-root");
  if (!root || !teamId) return;

  if (aTableReady(window.__lastATable)) {
    root.innerHTML = renderATableSection(window.__lastATable);
    return;
  }

  const token = ++aTableLoadToken;
  root.innerHTML = renderATableSection(null, { loading: true });

  const fetchFn =
    fetchWithTimeout ||
    ((url) => fetch(url, { signal: AbortSignal.timeout ? AbortSignal.timeout(45000) : undefined }));

  for (let attempt = 0; attempt < MAX_ATABLE_ATTEMPTS; attempt += 1) {
    try {
      const resp = await fetchFn(`${apiBase}/a-table?team_id=${teamId}`);
      const data = await resp.json();
      if (token !== aTableLoadToken) return;
      if (aTableReady(data)) {
        window.__lastATable = data;
        root.innerHTML = renderATableSection(data);
        return;
      }
      if (!data.loading && !data.refreshing && attempt > 2) {
        root.innerHTML = renderATableSection(null);
        return;
      }
    } catch (_) {
      if (token !== aTableLoadToken) return;
    }
    await new Promise((resolve) => setTimeout(resolve, ATABLE_POLL_MS));
  }

  if (token === aTableLoadToken) {
    root.innerHTML = renderATableSection(null, { loading: true, slow: true });
  }
}

function showATableFromMatchup(aTable) {
  const root = document.getElementById("a-table-root");
  if (!root) return;
  if (aTableReady(aTable)) {
    window.__lastATable = aTable;
    root.innerHTML = renderATableSection(aTable);
    return;
  }
  root.innerHTML = renderATableSection(null, { loading: true });
}
