"""Cross-league data integrity audit for MLB / NPB / CPBL.

Run: PYTHONPATH=. python scripts/audit_all_leagues.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class Issue:
    league: str
    check: str
    detail: str


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, league: str, check: str, detail: str) -> None:
        self.issues.append(Issue(league, check, detail))

    def note(self, text: str) -> None:
        self.notes.append(text)


def _counts_from_innings(my: list[int], opp: list[int]) -> tuple[list[int], list[int]]:
    scored = [i + 1 for i, r in enumerate(my[:9]) if (r or 0) > 0]
    allowed = [i + 1 for i, r in enumerate(opp[:9]) if (r or 0) > 0]
    return scored, allowed


def _agg(rows: list[dict[str, Any]], n: int) -> dict[str, Any]:
    scored = {str(i): 0 for i in range(1, 10)}
    allowed = {str(i): 0 for i in range(1, 10)}
    for row in rows[:n]:
        for inn in row.get("scoredInnings") or []:
            if 1 <= int(inn) <= 9:
                scored[str(inn)] += 1
        for inn in row.get("allowedInnings") or []:
            if 1 <= int(inn) <= 9:
                allowed[str(inn)] += 1
    return {"scored": scored, "allowed": allowed, "n": min(len(rows), n)}


def _compare_atable(
    report: Report,
    league: str,
    team_label: str,
    api: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    for key, n in (("recent5", 5), ("recent10", 10), ("recent20", 20)):
        block = api.get(key) or {}
        expect = _agg(rows, n)
        got_n = int(block.get("gameCount") or 0)
        if got_n != expect["n"]:
            report.fail(
                league,
                "a-table-count",
                f"{team_label} {key} gameCount={got_n} expected={expect['n']}",
            )
        for kind, api_key in (
            ("scored", "scoredCounts"),
            ("allowed", "allowedCounts"),
        ):
            got = block.get(api_key) or {}
            for inn in map(str, range(1, 10)):
                g = int(got.get(inn) or 0)
                e = int(expect[kind].get(inn) or 0)
                if g != e:
                    report.fail(
                        league,
                        "a-table-inning",
                        f"{team_label} {key} {kind} inning {inn}: got {g} expected {e}",
                    )


async def audit_cpbl(report: Report) -> None:
    from app.cpbl_service import (
        TEAM_BY_ID,
        CpblClient,
        _box_has_inning_data,
        _collect_batter_game_logs,
        _recent_batting_form,
        _recent_team_boxes,
        fetch_inning_comparison,
        fetch_matchup_starting_lineups,
        fetch_next_matchup,
    )
    from app.cpbl_risp_batting import get_risp_lookup, lookup_risp_avg
    from app.cpbl_verify import PITCHER_GOLDEN, SCHEDULE_MUST_INCLUDE

    client = CpblClient()
    try:
        schedule = await client.fetch_schedule_pool()
        snos = {g.get("gameSno") for g in schedule}
        for sno in SCHEDULE_MUST_INCLUDE:
            if sno not in snos:
                report.fail("CPBL", "schedule", f"missing gameSno {sno}")
        report.note(
            f"CPBL schedule games={len(schedule)} "
            f"finals={sum(1 for g in schedule if g.get('status')=='Final')}"
        )

        for case in PITCHER_GOLDEN:
            box = await client.fetch_box(case.game_sno, 2026)
            if not box:
                report.fail("CPBL", "pitcher-golden", f"no box sno={case.game_sno}")
                continue
            starters = {(box.get("awayStarter") or ""), (box.get("homeStarter") or "")}
            if not any(case.pitcher in s for s in starters):
                report.fail(
                    "CPBL",
                    "pitcher-golden",
                    f"sno={case.game_sno} {case.pitcher} not in starters {starters}",
                )

        # Deep A-table: first 2 teams (logic check); remaining teams window-only
        teams = list(TEAM_BY_ID.items())
        for team_id, team in teams[:2]:
            print(f"  CPBL deep {team['nameZh']}", flush=True)
            finished = [
                g
                for g in schedule
                if g.get("status") == "Final"
                and team_id in {g["awayTeamId"], g["homeTeamId"]}
                and g.get("gameSno") is not None
            ]
            finished.sort(key=lambda g: g.get("date", ""), reverse=True)
            rows: list[dict[str, Any]] = []
            for meta in finished[:60]:
                if len(rows) >= 20:
                    break
                box = await client.fetch_box(
                    int(meta["gameSno"]), int(meta.get("year") or 2026)
                )
                if not box or not _box_has_inning_data(box):
                    continue
                is_home = meta["homeTeamId"] == team_id
                side = "home" if is_home else "away"
                opp = "away" if is_home else "home"
                scored, allowed = _counts_from_innings(
                    box.get(f"{side}Innings") or [],
                    box.get(f"{opp}Innings") or [],
                )
                rows.append({"scoredInnings": scored, "allowedInnings": allowed})

            api = await fetch_inning_comparison(client, team_id)
            _compare_atable(report, "CPBL", team["nameZh"], api, rows)
            if len(finished) >= 20 and api["recent20"]["gameCount"] < 18:
                report.fail(
                    "CPBL",
                    "a-table-window",
                    f"{team['nameZh']} recent20={api['recent20']['gameCount']} "
                    f"finals={len(finished)}",
                )
            report.note(
                f"CPBL A-table {team['nameZh']}: "
                f"5={api['recent5']['gameCount']} "
                f"10={api['recent10']['gameCount']} "
                f"20={api['recent20']['gameCount']}"
            )

        for team_id, team in teams[2:]:
            print(f"  CPBL window {team['nameZh']}", flush=True)
            api = await fetch_inning_comparison(client, team_id)
            report.note(
                f"CPBL A-table {team['nameZh']}: "
                f"5={api['recent5']['gameCount']} "
                f"10={api['recent10']['gameCount']} "
                f"20={api['recent20']['gameCount']}"
            )
            if api["recent10"]["gameCount"] < 8 or api["recent20"]["gameCount"] < 15:
                report.fail(
                    "CPBL",
                    "a-table-window",
                    f"{team['nameZh']} 10={api['recent10']['gameCount']} "
                    f"20={api['recent20']['gameCount']}",
                )

        focus_id = next(iter(TEAM_BY_ID))
        print("  CPBL lineup check", flush=True)
        matchup = await fetch_next_matchup(client, focus_id)
        if matchup:
            lineups = await fetch_matchup_starting_lineups(client, matchup)
            for side in ("away", "home"):
                team_id = matchup[side]["teamId"]
                boxes = await _recent_team_boxes(client, team_id, limit=12)
                logs = _collect_batter_game_logs(boxes, team_id)
                batters = lineups.get(side, {}).get("batters") or []
                if len(batters) < 9:
                    report.fail(
                        "CPBL",
                        "lineup-size",
                        f"{matchup[side]['teamName']} batters={len(batters)}",
                    )
                for batter in batters:
                    acnt = str(batter.get("id") or "")
                    if not acnt:
                        continue
                    form = _recent_batting_form(logs.get(acnt) or [])
                    for key in ("recent3Avg", "recent5Avg"):
                        if form.get(key) and batter.get(key) and form[key] != batter[key]:
                            report.fail(
                                "CPBL",
                                "lineup-recent-avg",
                                f"{batter.get('name')} {key}: api={batter.get(key)} "
                                f"recomputed={form.get(key)}",
                            )
            report.note(
                f"CPBL lineup {matchup['away']['teamName']} vs "
                f"{matchup['home']['teamName']}"
            )

        risp = await get_risp_lookup(2026)
        if not risp:
            report.fail("CPBL", "risp", "empty RISP lookup")
        else:
            max_ab = max(int((v or {}).get("ab") or 0) for v in risp.values())
            report.note(f"CPBL RISP players={len(risp)} max_ab={max_ab}")
            if max_ab < 25:
                report.fail("CPBL", "risp", f"thin RISP cache max_ab={max_ab}")
            sample_acnt, sample_bucket = next(iter(risp.items()))
            avg = lookup_risp_avg(risp, sample_acnt)
            if int(sample_bucket.get("ab") or 0) > 0 and not avg:
                report.fail("CPBL", "risp", "lookup_risp_avg None for AB>0")
    finally:
        await client.close()


async def audit_mlb(report: Report) -> None:
    import httpx

    from app.mlb_service import (
        _recent_batting_form,
        _team_side,
        fetch_batter_hitting_game_log,
        fetch_inning_comparison,
        fetch_linescore,
        fetch_matchup_starting_lineups,
        fetch_next_matchup,
        fetch_recent_final_games,
    )
    from app.team_names import team_name_zh

    sample_ids = [136, 140]
    async with httpx.AsyncClient(timeout=60.0) as client:
        for team_id in sample_ids:
            games = await fetch_recent_final_games(team_id, 20, client=client)
            linescores = await asyncio.gather(
                *[fetch_linescore(client, g["gamePk"]) for g in games]
            )
            rows: list[dict[str, Any]] = []
            for game, lc in zip(games, linescores):
                side = _team_side(game, team_id)
                opp = "home" if side == "away" else "away"
                my: list[int] = []
                opp_runs: list[int] = []
                for inn in lc.get("innings") or []:
                    num = int(inn.get("num") or 0)
                    if num < 1 or num > 9:
                        continue
                    while len(my) < num:
                        my.append(0)
                    while len(opp_runs) < num:
                        opp_runs.append(0)
                    my[num - 1] = inn.get(side, {}).get("runs", 0) or 0
                    opp_runs[num - 1] = inn.get(opp, {}).get("runs", 0) or 0
                scored, allowed = _counts_from_innings(my, opp_runs)
                rows.append({"scoredInnings": scored, "allowedInnings": allowed})

            api = await fetch_inning_comparison(client, team_id)
            label = team_name_zh(team_id=team_id)
            _compare_atable(report, "MLB", label, api, rows)
            report.note(
                f"MLB A-table {label}: "
                f"5={api['recent5']['gameCount']} "
                f"10={api['recent10']['gameCount']} "
                f"20={api['recent20']['gameCount']}"
            )

        matchup = await fetch_next_matchup(client, 136)
        if not matchup:
            report.note("MLB: no upcoming Mariners matchup")
            return
        lineups = await fetch_matchup_starting_lineups(client, matchup)
        for side in ("away", "home"):
            for batter in ((lineups.get(side) or {}).get("batters") or [])[:5]:
                pid = batter.get("id")
                if not pid:
                    continue
                log = await fetch_batter_hitting_game_log(client, int(pid))
                form = _recent_batting_form(log)
                for key in ("recent3Avg", "recent5Avg", "recent3HitGames"):
                    if form.get(key) is None:
                        continue
                    if batter.get(key) is not None and batter.get(key) != form.get(key):
                        report.fail(
                            "MLB",
                            "lineup-recent",
                            f"{batter.get('name')} {key}: api={batter.get(key)} "
                            f"recomputed={form.get(key)}",
                        )
        has_risp = any(
            b.get("rispAvg")
            for side in ("away", "home")
            for b in (lineups.get(side) or {}).get("batters") or []
        )
        report.note(
            f"MLB lineup {matchup['away']['teamName']} vs "
            f"{matchup['home']['teamName']} RISP={has_risp}"
        )


async def audit_npb(report: Report) -> None:
    from app.npb_service import TEAM_BY_ID, NpbClient, fetch_inning_comparison

    client = NpbClient()
    try:
        schedule = await client.fetch_schedule()
        report.note(
            f"NPB schedule games={len(schedule)} "
            f"finals={sum(1 for g in schedule if g.get('status')=='Final')}"
        )
        samples = list(TEAM_BY_ID.items())[:2]
        for team_id, team in samples:
            print(f"  NPB A-table {team['nameZh']}", flush=True)
            finished = [
                g
                for g in schedule
                if g.get("status") == "Final"
                and team_id in {g.get("awayTeamId"), g.get("homeTeamId")}
                and g.get("href")
            ]
            finished.sort(key=lambda g: g.get("date", ""), reverse=True)
            finished = finished[:25]
            parsed_list = await asyncio.gather(
                *[client.fetch_game(m["href"]) for m in finished]
            )
            rows: list[dict[str, Any]] = []
            for meta, parsed in zip(finished, parsed_list):
                if not parsed:
                    continue
                if parsed.get("homeTeamId") is not None:
                    is_home = parsed["homeTeamId"] == team_id
                else:
                    is_home = meta["homeTeamId"] == team_id
                side = "home" if is_home else "away"
                opp = "away" if is_home else "home"
                my = parsed.get(f"{side}Innings") or []
                opp_i = parsed.get(f"{opp}Innings") or []
                if not any(my) and not any(opp_i) and sum(my) + sum(opp_i) == 0:
                    # may be all-zero shutout both ways — still valid if lengths ok
                    if not my and not opp_i:
                        continue
                scored, allowed = _counts_from_innings(my, opp_i)
                rows.append({"scoredInnings": scored, "allowedInnings": allowed})
                if len(rows) >= 20:
                    break

            api = await fetch_inning_comparison(client, team_id)
            _compare_atable(report, "NPB", team["nameZh"], api, rows)
            report.note(
                f"NPB A-table {team['nameZh']}: "
                f"5={api['recent5']['gameCount']} "
                f"10={api['recent10']['gameCount']} "
                f"20={api['recent20']['gameCount']} "
                f"recomputed={len(rows)}"
            )
    finally:
        await client.close()


async def main() -> int:
    # Unbuffered progress for long network audits.
    print("audit start", flush=True)
    report = Report()
    for name, fn in (
        ("MLB", audit_mlb),
        ("NPB", audit_npb),
        ("CPBL", audit_cpbl),
    ):
        print(f"=== Auditing {name} ===", flush=True)
        try:
            await fn(report)
        except Exception as exc:  # noqa: BLE001
            report.fail(name, "crash", repr(exc))
            print(f"{name} crash:", exc, flush=True)
        print(f"=== Done {name} issues so far={len(report.issues)} ===", flush=True)

    print("\n=== NOTES ===")
    for note in report.notes:
        print("-", note)

    print(f"\n=== ISSUES ({len(report.issues)}) ===")
    for issue in report.issues:
        print(f"[{issue.league}/{issue.check}] {issue.detail}")

    out = ROOT / "data" / "audit_all_leagues.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "issueCount": len(report.issues),
                "issues": [i.__dict__ for i in report.issues],
                "notes": report.notes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote", out)
    return 1 if report.issues else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
