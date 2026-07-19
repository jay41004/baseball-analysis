"""Background refresh of cached MLB analysis."""

from __future__ import annotations

import asyncio
import logging
import os

from app.cache import CACHE_VERSION, DEFAULT_GAMES, cached_team_count, load_from_disk, store_matchup
from app.mlb_service import analyze_matchup, fetch_teams

logger = logging.getLogger(__name__)

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3600"))
WARMUP_CONCURRENCY = int(os.environ.get("MLB_WARMUP_CONCURRENCY", "6"))

_refresh_lock = asyncio.Lock()
_refreshing_keys: set[str] = set()
_warming_all = False

def is_refreshing(team_id: int, games: int = DEFAULT_GAMES) -> bool:
    key = f"matchup:v{CACHE_VERSION}:{team_id}:{games}"
    return key in _refreshing_keys


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
    global _warming_all
    async with _refresh_lock:
        _warming_all = True
        try:
            teams = await fetch_teams()
            semaphore = asyncio.Semaphore(WARMUP_CONCURRENCY)

            async def refresh_one(team: dict) -> None:
                async with semaphore:
                    await refresh_matchup(team["id"], games)

            await asyncio.gather(*[refresh_one(team) for team in teams])
            logger.info("Finished warming MLB cache for %s teams", len(teams))
        finally:
            _warming_all = False


async def hourly_refresh_loop() -> None:
    while True:
        await refresh_all_matchups(DEFAULT_GAMES)
        await asyncio.sleep(REFRESH_SECONDS)


def is_warming_all() -> bool:
    return _warming_all


async def start_cache_services() -> None:
    load_from_disk()
    logger.info(
        "MLB cache loaded (%s teams on disk). Starting background warm-up.",
        cached_team_count(DEFAULT_GAMES),
    )
    asyncio.create_task(hourly_refresh_loop())
    asyncio.create_task(refresh_all_matchups(DEFAULT_GAMES))