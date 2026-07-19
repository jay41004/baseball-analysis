function aTableCell(count, total) {
  if (!total) return "0(0%)";
  return `${count}(${Math.round((count / total) * 100)}%)`;
}

function renderATableBlock(side, roleLabel) {
  if (!side?.recent10 || !side?.recent20) {
    return `<p class="empty-note">尚無 a表格 數據</p>`;
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

function renderATableSection(aTable) {
  const awayBlock =
    aTable?.away?.recent10 && aTable?.away?.recent20
      ? renderATableBlock(aTable.away, "客隊")
      : `<p class="empty-note">a表格 資料載入中… 若久未出現請按「立即更新」</p>`;
  const homeBlock =
    aTable?.home?.recent10 && aTable?.home?.recent20
      ? renderATableBlock(aTable.home, "主隊")
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
