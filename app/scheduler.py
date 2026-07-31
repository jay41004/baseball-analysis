"""Background refresh of cached MLB analysis."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.cache import (
    CACHE_VERSION,
    DEFAULT_GAMES,
    cached_team_count,
    get_a_table,
    get_matchup,
    is_stale,
    load_from_disk,
    store_matchup,
    store_a_table,
)
from app.mlb_service import analyze_matchup, analyze_matchup_a_table, fetch_teams

logger = logging.getLogger(__name__)

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3600"))
WARMUP_CONCURRENCY = int(os.environ.get("MLB_WARMUP_CONCURRENCY", "2"))

_refresh_lock = asyncio.Lock()
_refreshing_keys: set[str] = set()
_warming_all = False

def is_refreshing(team_id: int, games: int = DEFAULT_GAMES) -> bool:
    key = f"matchup:v{CACHE_VERSION}:{team_id}:{games}"
    return key in _refreshing_keys


_refreshing_a_table: set[int] = set()
_a_table_done: dict[int, asyncio.Event] = {}


def is_refreshing_a_table(team_id: int) -> bool:
    return team_id in _refreshing_a_table


async def refresh_a_table(team_id: int) -> None:
    if team_id in _refreshing_a_table:
        waiter = _a_table_done.get(team_id)
        if waiter is not None:
            await waiter.wait()
        return

    done = asyncio.Event()
    _a_table_done[team_id] = done
    _refreshing_a_table.add(team_id)
    try:
        data = await analyze_matchup_a_table(team_id)
        await store_a_table(team_id, data)
        logger.info("Refreshed MLB a-table cache for team %s", team_id)
    except Exception:
        logger.exception("Failed to refresh MLB a-table for team %s", team_id)
    finally:
        _refreshing_a_table.discard(team_id)
        done.set()
        _a_table_done.pop(team_id, None)


async def ensure_a_table(team_id: int, *, force: bool = False) -> dict[str, Any]:
    cached = get_a_table(team_id)
    if cached and not force:
        return cached
    await refresh_a_table(team_id)
    cached = get_a_table(team_id)
    if not cached:
        raise ValueError(f"無法產生 MLB a 表格（team {team_id}）")
    return cached


async def refresh_matchup(team_id: int, games: int = DEFAULT_GAMES) -> None:
    key = f"matchup:v{CACHE_VERSION}:{team_id}:{games}"
    if key in _refreshing_keys:
        return

    _refreshing_keys.add(key)
    try:
        data = await analyze_matchup(team_id, games)
        await store_matchup(team_id, games, data)
        if a_table := data.get("aTable"):
            await store_a_table(team_id, a_table)
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
    startup_delay = int(os.environ.get("WARMUP_START_DELAY", "600" if is_cloud else "300"))
    if startup_delay > 0:
        await asyncio.sleep(startup_delay)

    while True:
        await refresh_all_matchups(DEFAULT_GAMES)
        await asyncio.sleep(REFRESH_SECONDS)


def is_warming_all() -> bool:
    return _warming_all


async def start_cache_services(*, skip_load: bool = False) -> None:
    if not skip_load:
        load_from_disk()
    logger.info(
        "MLB cache loaded (%s teams on disk).",
        cached_team_count(DEFAULT_GAMES),
    )
    from app.cloud_lite import is_cloud_lite

    if not is_cloud_lite():
        asyncio.create_task(hourly_refresh_loop())