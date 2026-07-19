"""Background refresh of cached MLB analysis."""

from __future__ import annotations

import asyncio
import logging
import os

from app.cache import (
    CACHE_VERSION,
    DEFAULT_GAMES,
    cached_team_count,
    get_matchup,
    is_stale,
    load_from_disk,
    store_matchup,
    store_a_table,
)
from app.mlb_service import analyze_matchup, analyze_matchup_a_table, fetch_teams

logger = logging.getLogger(__name__)

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3600"))
WARMUP_CONCURRENCY = int(os.environ.get("MLB_WARMUP_CONCURRENCY", "6"))

_refresh_lock = asyncio.Lock()
_refreshing_keys: set[str] = set()
_warming_all = False

def is_refreshing(team_id: int, games: int = DEFAULT_GAMES) -> bool:
    key = f"matchup:v{CACHE_VERSION}:{team_id}:{games}"
    return key in _refreshing_keys


_refreshing_a_table: set[int] = set()


def is_refreshing_a_table(team_id: int) -> bool:
    return team_id in _refreshing_a_table


async def refresh_a_table(team_id: int) -> None:
    if team_id in _refreshing_a_table:
        return
    _refreshing_a_table.add(team_id)
    try:
        data = await analyze_matchup_a_table(team_id)
        await store_a_table(team_id, data)
        logger.info("Refreshed MLB a-table cache for team %s", team_id)
    except Exception:
        logger.exception("Failed to refresh MLB a-table for team %s", team_id)
    finally:
        _refreshing_a_table.discard(team_id)


async def refresh_matchup(team_id: int, games: int = DEFAULT_GAMES) -> None:
    key = f"matchup:v{CACHE_VERSION}:{team_id}:{games}"
    if key in _refreshing_keys:
        return

    _refreshing_keys.add(key)
    try:
        data = await analyze_matchup(team_id, games)
        await store_matchup(team_id, games, data)
        asyncio.create_task(refresh_a_table(team_id))
        logger.info("Refreshed matchup cache for team %s (%s games)", team_id, games)
    except Exception:
        logger.exception("Failed to refresh matchup for team %s", team_id)
    finally:
        _refreshing_keys.discard(key)


def _teams_needing_refresh(teams: list[dict], games: int) -> list[dict]:
    stale: list[dict] = []
    for team in teams:
        entry = get_matchup(team["id"], games)
        if entry is None or is_stale(entry["updatedAt"]):
            stale.append(team)
    return stale


async def refresh_all_matchups(games: int = DEFAULT_GAMES) -> None:
    global _warming_all
    async with _refresh_lock:
        _warming_all = True
        try:
            teams = await fetch_teams()
            targets = _teams_needing_refresh(teams, games)
            if not targets:
                logger.info("MLB cache already warm for all %s teams", len(teams))
                return

            semaphore = asyncio.Semaphore(WARMUP_CONCURRENCY)

            async def refresh_one(team: dict) -> None:
                async with semaphore:
                    await refresh_matchup(team["id"], games)

            await asyncio.gather(*[refresh_one(team) for team in targets])
            logger.info(
                "Finished warming MLB cache (%s/%s teams refreshed)",
                len(targets),
                len(teams),
            )
        finally:
            _warming_all = False


async def hourly_refresh_loop() -> None:
    is_cloud = bool(os.environ.get("RENDER"))
    startup_delay = int(os.environ.get("WARMUP_START_DELAY", "0" if is_cloud else "120"))
    if startup_delay > 0:
        await asyncio.sleep(startup_delay)

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