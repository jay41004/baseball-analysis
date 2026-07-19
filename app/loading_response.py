"""Placeholder API payloads while background refresh runs."""

from __future__ import annotations

from typing import Any

from app.inning_comparison import empty_inning_comparison


def _empty_summary() -> dict[str, Any]:
    return {
        "totalGames": 0,
        "over15": 0,
        "under15": 0,
        "over25": 0,
        "under25": 0,
        "avgRuns": 0,
        "firstInningScored": 0,
        "firstInningNoScore": 0,
    }


def _loading_side() -> dict[str, Any]:
    return {
        "teamName": "載入中…",
        "games": [],
        "summary": _empty_summary(),
        "probablePitcher": None,
        "pitcherAnalysis": None,
    }


def loading_matchup_payload(team_id: int, *, cache_version: int) -> dict[str, Any]:
    return {
        "focusTeamId": team_id,
        "matchup": {
            "date": "—",
            "gameDate": None,
            "status": "資料準備中",
        },
        "away": _loading_side(),
        "home": _loading_side(),
        "aTable": {
            "away": empty_inning_comparison(),
            "home": empty_inning_comparison(),
        },
        "loading": True,
        "refreshing": True,
        "cacheVersion": cache_version,
        "fromCache": False,
        "cachedAt": None,
        "nextRefreshAt": None,
    }
