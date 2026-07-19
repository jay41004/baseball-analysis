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
    games10 = games[:10]
    games20 = games[:20]
    scored10, allowed10 = counts_from_game_rows(games10)
    scored20, allowed20 = counts_from_game_rows(games20)
    return {
        "teamName": team_name,
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


def empty_inning_comparison(team_name: str = "載入中…") -> dict[str, Any]:
    empty = empty_inning_counts()
    return {
        "teamName": team_name,
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
