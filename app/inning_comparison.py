"""Near-10 vs near-20 per-inning scored/allowed counts (a表格 payload)."""

from __future__ import annotations

from typing import Any


def empty_inning_counts() -> dict[str, int]:
    return {str(inning): 0 for inning in range(1, 10)}


def counts_from_game_rows(games: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    scored = {inning: 0 for inning in range(1, 10)}
    allowed = {inning: 0 for inning in range(1, 10)}
    for game in games:
        for inning in game.get("scoredInnings") or []:
            if 1 <= inning <= 9:
                scored[inning] += 1
        for inning in game.get("allowedInnings") or []:
            if 1 <= inning <= 9:
                allowed[inning] += 1
    return (
        {str(inning): scored[inning] for inning in range(1, 10)},
        {str(inning): allowed[inning] for inning in range(1, 10)},
    )


def build_inning_comparison(team_name: str, games: list[dict[str, Any]]) -> dict[str, Any]:
    games5 = games[:5]
    games10 = games[:10]
    games20 = games[:20]
    scored5, allowed5 = counts_from_game_rows(games5)
    scored10, allowed10 = counts_from_game_rows(games10)
    scored20, allowed20 = counts_from_game_rows(games20)
    return {
        "teamName": team_name,
        "recent5": {
            "gameCount": len(games5),
            "scoredCounts": scored5,
            "allowedCounts": allowed5,
        },
        "recent10": {
            "gameCount": len(games10),
            "scoredCounts": scored10,
            "allowedCounts": allowed10,
        },
        "recent20": {
            "gameCount": len(games20),
            "scoredCounts": scored20,
            "allowedCounts": allowed20,
        },
    }


def a_table_payload_complete(payload: dict[str, Any]) -> bool:
    for side_key in ("away", "home"):
        side = payload.get(side_key)
        if not side or "recent5" not in side:
            return False
        if not side.get("recent10") or not side.get("recent20"):
            return False
    return True


def empty_inning_comparison(team_name: str = "載入中…") -> dict[str, Any]:
    empty = empty_inning_counts()
    return {
        "teamName": team_name,
        "recent5": {
            "gameCount": 0,
            "scoredCounts": dict(empty),
            "allowedCounts": dict(empty),
        },
        "recent10": {
            "gameCount": 0,
            "scoredCounts": dict(empty),
            "allowedCounts": dict(empty),
        },
        "recent20": {
            "gameCount": 0,
            "scoredCounts": dict(empty),
            "allowedCounts": dict(empty),
        },
    }


def empty_situational() -> dict[str, Any]:
    empty = empty_inning_counts()
    return {
        "awayPitcherAwayStarts": {
            "pitcherName": "",
            "gameCount": 0,
            "allowedCounts": dict(empty),
        },
        "homePitcherHomeStarts": {
            "pitcherName": "",
            "gameCount": 0,
            "allowedCounts": dict(empty),
        },
        "awayTeamAwayGames": {
            "teamName": "",
            "gameCount": 0,
            "scoredCounts": dict(empty),
        },
        "homeTeamHomeGames": {
            "teamName": "",
            "gameCount": 0,
            "scoredCounts": dict(empty),
        },
    }


def _pitcher_situational_block(
    panel: dict[str, Any], is_home: bool, *, limit: int = 10
) -> dict[str, Any]:
    probable = panel.get("probablePitcher") or {}
    fallback_name = probable.get("fullName") or ""
    analysis = panel.get("pitcherAnalysis")
    if not analysis:
        return {
            "pitcherName": fallback_name,
            "gameCount": 0,
            "allowedCounts": empty_inning_counts(),
        }

    pool = analysis.get("_startPool") or analysis.get("games") or []
    rows = [row for row in pool if bool(row.get("isHome")) == is_home][:limit]
    allowed_counts = empty_inning_counts()
    for row in rows:
        scored = set(row.get("scoredInnings") or [])
        if row.get("firstInningScored"):
            scored.add(1)
        for inning in scored:
            if 1 <= inning <= 9:
                allowed_counts[str(inning)] += 1

    return {
        "pitcherName": analysis.get("pitcherName") or fallback_name,
        "gameCount": len(rows),
        "allowedCounts": allowed_counts,
    }


def _team_situational_block(
    panel: dict[str, Any], is_home: bool, *, limit: int = 10
) -> dict[str, Any]:
    pool = panel.get("_scoredPool") or []
    rows = [row for row in pool if bool(row.get("isHome")) == is_home][:limit]
    scored_counts, _ = counts_from_game_rows(rows)
    return {
        "teamName": panel.get("teamName") or "",
        "gameCount": len(rows),
        "scoredCounts": scored_counts,
    }


def build_matchup_situational(
    away_panel: dict[str, Any], home_panel: dict[str, Any]
) -> dict[str, Any]:
    return {
        "awayPitcherAwayStarts": _pitcher_situational_block(away_panel, False),
        "homePitcherHomeStarts": _pitcher_situational_block(home_panel, True),
        "awayTeamAwayGames": _team_situational_block(away_panel, False),
        "homeTeamHomeGames": _team_situational_block(home_panel, True),
    }


def strip_panel_internals(panel: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in panel.items() if key != "_scoredPool"}
    analysis = cleaned.get("pitcherAnalysis")
    if isinstance(analysis, dict) and "_startPool" in analysis:
        pitcher_copy = dict(analysis)
        pitcher_copy.pop("_startPool", None)
        cleaned["pitcherAnalysis"] = pitcher_copy
    return cleaned
