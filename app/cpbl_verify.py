"""Automated CPBL data verification against stats.cpbl.com.tw."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.cpbl_service import (
    CpblClient,
    _pitcher_row_from_box,
    analyze_pitcher_starts,
    invalidate_shared_schedule_cache,
)
from app.cpbl_stats import (
    _discover_max_game_sno,
    _fetch_stats_game_html,
    _parse_stats_game_page,
    parse_stats_starters,
)


@dataclass(frozen=True)
class PitcherGolden:
    game_sno: int
    pitcher: str
    team_id: int
    scored_innings: list[int]
    earned_runs: int
    first_five_runs: int


# Regression cases discovered in production debugging.
PITCHER_GOLDEN: tuple[PitcherGolden, ...] = (
    PitcherGolden(154, "蔣銲", 5, [7], 3, 0),
    PitcherGolden(158, "阿部雄大", 4, [3], 2, 2),
    PitcherGolden(168, "阿部雄大", 4, [6], 3, 0),
    PitcherGolden(214, "蔣銲", 5, [4], 1, 1),
    PitcherGolden(194, "蔣銲", 5, [6], 1, 0),
)

SCHEDULE_MUST_INCLUDE: tuple[int, ...] = (154, 168, 214, 226)


@dataclass
class VerifyIssue:
    check: str
    detail: str


async def _stats_final_snos_and_starters(
    http: httpx.AsyncClient, year: int = 2026
) -> tuple[set[int], dict[str, set[int]]]:
    max_sno = await _discover_max_game_sno(http, year)
    finals: set[int] = set()
    by_name: dict[str, set[int]] = {}
    for sno in range(1, max_sno + 1):
        html = await _fetch_stats_game_html(http, sno, year)
        if not html:
            continue
        parsed = _parse_stats_game_page(html, game_sno=sno, year=year)
        if parsed and parsed.get("status") == "Final":
            finals.add(sno)
        away, home = parse_stats_starters(html, game_sno=sno, year=year)
        for name in (away, home):
            if name:
                by_name.setdefault(name, set()).add(sno)
    return finals, by_name


async def verify_cpbl(*, reset_schedule_cache: bool = False) -> list[VerifyIssue]:
    issues: list[VerifyIssue] = []

    if reset_schedule_cache:
        invalidate_shared_schedule_cache()

    client = CpblClient()
    try:
        schedule = await client.fetch_schedule_pool()
        by_sno = {g["gameSno"]: g for g in schedule if g.get("gameSno") is not None}

        for sno in SCHEDULE_MUST_INCLUDE:
            if sno not in by_sno:
                issues.append(VerifyIssue("schedule", f"missing required game G{sno}"))

        async with httpx.AsyncClient(timeout=180) as http:
            stats_finals, stats_starters = await _stats_final_snos_and_starters(http)

        pool_finals = {
            g["gameSno"]
            for g in schedule
            if g.get("status") == "Final" and g.get("gameSno") is not None
        }
        missing = sorted(stats_finals - pool_finals)
        if missing:
            issues.append(
                VerifyIssue(
                    "schedule",
                    f"{len(missing)} finals missing from pool, e.g. G{missing[:5]}",
                )
            )

        for case in PITCHER_GOLDEN:
            meta = by_sno.get(case.game_sno)
            if not meta:
                issues.append(VerifyIssue("pitcher", f"G{case.game_sno} not in schedule"))
                continue
            box = await client.fetch_box(case.game_sno, int(meta.get("year") or 2026))
            if not box:
                issues.append(VerifyIssue("pitcher", f"G{case.game_sno} box score missing"))
                continue
            row = _pitcher_row_from_box(meta, box, case.team_id, case.pitcher)
            scored = row.get("scoredInnings") or []
            er = row.get("earnedRuns")
            f5 = row.get("firstFiveRunsAllowed")
            runs_sum = sum(row.get("runsByInning") or [])

            if scored != case.scored_innings:
                issues.append(
                    VerifyIssue(
                        "pitcher",
                        f"G{case.game_sno} {case.pitcher} scored innings "
                        f"{scored} != expected {case.scored_innings}",
                    )
                )
            if er != case.earned_runs:
                issues.append(
                    VerifyIssue(
                        "pitcher",
                        f"G{case.game_sno} {case.pitcher} ER {er} != expected {case.earned_runs}",
                    )
                )
            if f5 != case.first_five_runs:
                issues.append(
                    VerifyIssue(
                        "pitcher",
                        f"G{case.game_sno} {case.pitcher} F5 {f5} != expected {case.first_five_runs}",
                    )
                )
            if er and runs_sum == 0:
                issues.append(
                    VerifyIssue(
                        "pitcher",
                        f"G{case.game_sno} {case.pitcher} ER={er} but all inning runs are zero",
                    )
                )

        for name, team_id in (("阿部雄大", 4), ("蔣銲", 5)):
            stats_snos = stats_starters.get(name, set())
            result = await analyze_pitcher_starts(client, name, team_id, 30, scan_limit=80)
            app_snos = {g["gameSno"] for g in result.get("_startPool") or []}
            missing_starts = sorted(stats_snos - app_snos)
            if missing_starts:
                issues.append(
                    VerifyIssue(
                        "pitcher_starts",
                        f"{name} missing {len(missing_starts)} starts, e.g. G{missing_starts[:5]}",
                    )
                )
    finally:
        await client.close()

    return issues


async def verify_cpbl_or_raise(*, reset_schedule_cache: bool = False) -> None:
    issues = await verify_cpbl(reset_schedule_cache=reset_schedule_cache)
    if issues:
        lines = "\n".join(f"- [{item.check}] {item.detail}" for item in issues)
        raise AssertionError(f"CPBL verification failed:\n{lines}")
