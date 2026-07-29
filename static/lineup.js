function lineupSourceLabel(side) {
  if (!side?.batters?.length) return "尚無打線資料";
  if (side.source === "confirmed") return "本場先發打序";
  const date = side.sourceDate ? side.sourceDate.slice(5) : "";
  return date ? `上一場先發（${date}）` : "上一場先發打序";
}

function formatStat(value) {
  if (value == null || value === "") return "—";
  if (typeof value === "string" && value.startsWith("..")) {
    return value.slice(1);
  }
  return value;
}

function batterLabel(batter) {
  const order = batter.order ?? "";
  const name = batter.name ?? "—";
  const pos = batter.positionLabel || batter.position || "";
  if (pos) return `${order}. ${name} ${pos}`;
  return `${order}. ${name}`;
}

function formatAbHits(batter) {
  if (batter.abHits) return batter.abHits;
  if (batter.atBats != null && batter.hits != null) {
    return `${batter.atBats}-${batter.hits}`;
  }
  return "—";
}

function formatRecentHits(batter) {
  const games = batter.recent3Games ?? 0;
  const hits = batter.recent3HitGames;
  if (!games || hits == null) return "—";
  return `${hits}/${games}`;
}

function formatRecentAvg(value) {
  if (value == null || value === "") return "—";
  return value.startsWith(".") ? value : `.${value}`;
}

function pitcherNote(side) {
  const name = side?.opposingPitcher?.fullName;
  return name ? `對手投手：${name}` : "";
}

function renderLineupBlock(side, roleLabel) {
  if (!side?.batters?.length) {
    return `<p class="empty-note">${roleLabel} · ${side?.teamName ?? "—"}：尚無打線資料</p>`;
  }

  const rows = side.batters
    .map(
      (batter) => `
        <tr>
          <td class="col-name">${batterLabel(batter)}</td>
          <td class="col-stat">${formatStat(batter.avg)}</td>
          <td class="col-stat">${formatRecentAvg(batter.rispAvg)}</td>
          <td class="col-stat">${formatStat(batter.homeRuns)}</td>
          <td class="col-stat">${formatStat(batter.rbi)}</td>
          <td class="col-stat">${formatStat(batter.vsPitcherSeasonAvg)}</td>
          <td class="col-stat">${formatStat(batter.vsPitcherCareerAvg)}</td>
          <td class="col-stat">${formatRecentHits(batter)}</td>
          <td class="col-stat">${formatRecentAvg(batter.recent3Avg)}</td>
          <td class="col-stat">${formatRecentAvg(batter.recent5Avg)}</td>
        </tr>
      `
    )
    .join("");

  const pitcher = pitcherNote(side);

  return `
    <article class="lineup-block">
      <h4 class="lineup-team">${roleLabel} · ${side.teamName}</h4>
      <p class="lineup-source">${lineupSourceLabel(side)}${pitcher ? ` · ${pitcher}` : ""}</p>
      <div class="table-wrap">
        <table class="data-table lineup-table lineup-table-wide">
          <thead>
            <tr>
              <th class="col-name">打者</th>
              <th class="col-stat">打擊率</th>
              <th class="col-stat">得點圈<br>打擊率</th>
              <th class="col-stat">全壘打</th>
              <th class="col-stat">打點</th>
              <th class="col-stat">對投手<br>今年</th>
              <th class="col-stat">對投手<br>生涯</th>
              <th class="col-stat">近3<br>安打</th>
              <th class="col-stat">近3<br>AVG</th>
              <th class="col-stat">近5<br>AVG</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </article>
  `;
}

function renderLineupSection(lineups) {
  const awayBlock = renderLineupBlock(lineups?.away, "客隊");
  const homeBlock = renderLineupBlock(lineups?.home, "主隊");

  return `
    <details class="lineup-section card" open>
      <summary class="lineup-summary">先發打線 · 本季成績</summary>
      <p class="lineup-note">若本場尚未公布先發，顯示該隊上一場比賽的先發打序。得點圈打擊率 = 二、三壘有人時的打擊率；對投手成績為對本場先發投手；近3安打 = 近3場有安打的場數。</p>
      <div class="lineup-grid">
        ${awayBlock}
        ${homeBlock}
      </div>
    </details>
  `;
}

function paintLineup(lineups) {
  const root = document.getElementById("lineup-root");
  if (!root) return;
  root.innerHTML = renderLineupSection(lineups || {});
}

function syncLineup(lineups) {
  paintLineup(lineups);
}
