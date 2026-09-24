"""Detect when cached recent-game panels lag behind live Final results."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def latest_game_date_in_pitcher_analysis(analysis: dict[str, Any] | None) -> str | None:
    """Newest YYYY-MM-DD in pitcherAnalysis.games."""
    if not analysis:
        return None
    latest: str | None = None
    for game in analysis.get("games") or []:
        raw = (game.get("date") or "")[:10]
        if raw and (latest is None or raw > latest):
            latest = raw
    return latest


def latest_game_date_in_data(data: dict[str, Any] | None) -> str | None:
    """Newest YYYY-MM-DD across away/home game panels."""
    if not data:
        return None
    latest: str | None = None
    for side in ("away", "home"):
        for game in (data.get(side) or {}).get("games") or []:
            raw = (game.get("date") or "")[:10]
            if raw and (latest is None or raw > latest):
                latest = raw
    return latest


def _is_behind(cached_latest: str | None, live_latest: str | None) -> bool:
    if not live_latest:
        return False
    if not cached_latest:
        return True
    return live_latest > cached_latest


async def mlb_pitcher_analysis_stale(
    client: httpx.AsyncClient, data: dict[str, Any]
) -> bool:
    """True when a listed starter has newer completed starts than cached rows."""
    from datetime import date

    from app.mlb_service import fetch_pitcher_starts, resolve_pitcher_id
    from app.pitcher_peer_sync import pitcher_name
    from app.pitcher_rows import pitcher_starter_mismatch

    if pitcher_starter_mismatch(data):
        return True

    today = date.today().isoformat()
    for side in ("away", "home"):
        panel = data.get(side) or {}
        starter = pitcher_name(panel).strip()
        if not starter:
            continue
        analysis = panel.get("pitcherAnalysis") or {}
        cached_latest = latest_game_date_in_pitcher_analysis(analysis)
        if not cached_latest:
            continue
        probable = panel.get("probablePitcher") or {}
        pitcher_id = await resolve_pitcher_id(client, probable.get("id"), starter)
        if not pitcher_id:
            continue
        starts = await fetch_pitcher_starts(client, pitcher_id, 8)
        live_dates = [(s.get("date") or "")[:10] for s in starts if (s.get("date") or "")]
        if not live_dates:
            continue
        live_latest = max(live_dates)
        if live_latest > cached_latest and live_latest < today:
            return True
    return False


async def mlb_team_panels_stale(
    client: httpx.AsyncClient, team_id: int, data: dict[str, Any]
) -> bool:
    from app.mlb_display import mlb_game_row_date
    from app.mlb_service import fetch_recent_final_games

    cached_latest = latest_game_date_in_data(data)
    finals = await fetch_recent_final_games(team_id, 3, client=client)
    if not finals:
        return False
    live_latest = max(mlb_game_row_date(g) for g in finals)
    return _is_behind(cached_latest, live_latest)


async def npb_team_panels_stale(team_id: int, data: dict[str, Any]) -> bool:
    from app.npb_service import NpbClient

    cached_latest = latest_game_date_in_data(data)
    client = NpbClient()
    try:
        schedule = await client.fetch_schedule()
    finally:
        await client.close()
    finals = [
        g
        for g in schedule
        if g.get("status") == "Final"
        and team_id in {g.get("awayTeamId"), g.get("homeTeamId")}
    ]
    if not finals:
        return False
    live_latest = max((g.get("date") or "")[:10] for g in finals)
    return _is_behind(cached_latest, live_latest)


async def cpbl_team_panels_stale(team_id: int, data: dict[str, Any]) -> bool:
    from app.cpbl_service import CpblClient

    cached_latest = latest_game_date_in_data(data)
    client = CpblClient()
    try:
        schedule = await client.fetch_schedule_pool()
    finally:
        await client.close()
    finals = [
        g
        for g in schedule
        if g.get("status") == "Final"
        and team_id in {g.get("awayTeamId"), g.get("homeTeamId")}
    ]
    if not finals:
        return False
    live_latest = max((g.get("date") or "")[:10] for g in finals)
    return _is_behind(cached_latest, live_latest)


async def mlb_matchup_content_stale(
    team_id: int, data: dict[str, Any]
) -> bool:
    """True when team panels or pitcher per-start rows lag live MLB."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if await mlb_team_panels_stale(client, team_id, data):
                return True
            return await mlb_pitcher_analysis_stale(client, data)
    except Exception:
        logger.exception("MLB content stale check failed for team %s", team_id)
    return False


async def panels_stale_for_league(
    league: str, team_id: int, data: dict[str, Any]
) -> bool:
    try:
        if league == "mlb":
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await mlb_team_panels_stale(client, team_id, data)
        if league == "npb":
            return await npb_team_panels_stale(team_id, data)
        if league == "cpbl":
            return await cpbl_team_panels_stale(team_id, data)
    except Exception:
        logger.exception("panels_stale_for_league failed %s team %s", league, team_id)
    return False


def panel_staleness_issue(
    league: str, team_id: int, data: dict[str, Any], live_latest: str | None
) -> str | None:
    cached_latest = latest_game_date_in_data(data)
    if _is_behind(cached_latest, live_latest):
        return (
            f"{league} team {team_id}: recent panels stale "
            f"cached_latest={cached_latest} live_latest={live_latest}"
        )
    return None
