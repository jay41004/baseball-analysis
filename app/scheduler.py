"""Background refresh of cached MLB analysis."""

from __future__ import annotations

import asyncio
import logging

from app.cache import CACHE_VERSION, DEFAULT_GAMES, load_from_disk, store_matchup
from app.mlb_service import analyze_matchup, fetch_teams

logger = logging.getLogger(__name__)

_refresh_lock = asyncio.Lock()
_refreshing_keys: set[str] = set()


async def refresh_matchup(team_id: int, games: int = DEFAULT_GAMES) -> None:
    key = f"matchup:v{CACHE_VERSION}:{team_id}:{games}"
    if key in _refreshing_keys:
        return

    _refreshing_keys.add(key)
    try:
        data = await analyze_matchup(team_id, games)
        await store_matchup(team_id, games, data)
        logger.info("Refreshed matchup cache for team %s (%s games)", team_id, games)
    except Exception:
        logger.exception("Failed to refresh matchup for team %s", team_id)
    finally:
        _refreshing_keys.discard(key)


async def refresh_all_matchups(games: int = DEFAULT_GAMES) -> None:
    async with _refresh_lock:
        teams = await fetch_teams()
        for index, team in enumerate(teams):
            await refresh_matchup(team["id"], games)
            if index < len(teams) - 1:
                await asyncio.sleep(0.5)


async def hourly_refresh_loop() -> None:
    while True:
        await refresh_all_matchups(DEFAULT_GAMES)
        await asyncio.sleep(3600)


async def start_cache_services() -> None:
    load_from_disk()
    asyncio.create_task(hourly_refresh_loop())
    asyncio.create_task(refresh_all_matchups(DEFAULT_GAMES))
