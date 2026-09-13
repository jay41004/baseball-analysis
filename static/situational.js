function renderInningCountBlock(block, title) {
  const counts = block?.allowedCounts || block?.scoredCounts || {};
  const total = block?.gameCount || 0;
  const label = block?.pitcherName || block?.teamName || "";
  const subtitle = label ? `${label} · 近${total}場` : total ? `近${total}場` : "";

  if (!total) {
    return `
      <article class="a-table-block">
        <h4 class="a-table-team">${title}</h4>
        <p class="empty-note">${label ? `${label} · ` : ""}尚無數據</p>
      </article>
    `;
  }

  const rows = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    .map(
      (inning) => `
        <tr>
          <td class="col-inning">${inning}</td>
          <td>${aTableCell(counts[String(inning)] ?? 0, total)}</td>
        </tr>
      `
    )
    .join("");

  return `
    <article class="a-table-block">
      <h4 class="a-table-team">${title}</h4>
      ${subtitle ? `<p class="situational-subtitle">${subtitle}</p>` : ""}
      <div class="table-wrap">
        <table class="data-table a-table situational-table">
          <thead>
            <tr>
              <th class="col-inning">局</th>
              <th>場數(比例)</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </article>
  `;
}

function renderSituationalSection(situational) {
  const root = document.getElementById("situational-root");
  if (!root) return;

  if (!situational) {
    root.innerHTML = "";
    return;
  }

  root.innerHTML = `
    <details class="a-table-section situational-section card" open>
      <summary class="a-table-summary">客/主場情境 · 各局失分/得分場數</summary>
      <p class="a-table-note">
        客/主場先發：僅統計該投手在客場或主場的先發；球隊：僅統計客場或主場比賽（最多近10場）。
        數字為該局有失分或得分的場數，括號為比例。
      </p>
      <div class="a-table-grid">
        ${renderInningCountBlock(
          situational.awayPitcherAwayStarts,
          "客場先發 · 各局失分場數"
        )}
        ${renderInningCountBlock(
          situational.homePitcherHomeStarts,
          "主場先發 · 各局失分場數"
        )}
        ${renderInningCountBlock(
          situational.awayTeamAwayGames,
          "客隊客場 · 各局得分場數"
        )}
        ${renderInningCountBlock(
          situational.homeTeamHomeGames,
          "主隊主場 · 各局得分場數"
        )}
      </div>
    </details>
  `;
}
