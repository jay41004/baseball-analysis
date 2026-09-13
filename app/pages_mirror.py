"""Pull GitHub Pages static JSON into Render (cloud-lite sync).

GitHub Actions rebuilds docs/data every few hours. Render mirrors that snapshot
so user requests never block on live MLB/NPB/CPBL full rebuilds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PAGES_DATA_BASE = os.environ.get(
    "PAGES_DATA_BASE",
    "https://jay41004.github.io/baseball-analysis/data",
).rstrip("/")

_WRAPPER_KEYS = {
    "cachedAt",
    "nextRefreshAt",
    "fromCache",
    "refreshing",
    "loading",
}

_SLATE_CACHE: dict[str, tuple[float, dict[str, list[dict[str, Any]]]]] = {}
_SLATE_TTL_S = 300.0


def cache_data_from_pages_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _WRAPPER_KEYS}


async def fetch_pages_json(path: str, *, timeout: float = 20.0) -> dict[str, Any] | None:
    url = f"{PAGES_DATA_BASE}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "baseball-analysis-mirror/1"})
            if resp.status_code != 200:
                logger.warning("Pages miss %s → %s", url, resp.status_code)
                return None
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
    except Exception:
        logger.exception("Failed to fetch Pages JSON %s", url)
        return None


async def fetch_pages_matchup(
    league: str, team_id: int, games: int = 10
) -> dict[str, Any] | None:
    payload = await fetch_pages_json(f"{league}/matchup_{team_id}_{games}.json")
    if not payload or not payload.get("matchup"):
        return None
    return cache_data_from_pages_payload(payload)


async def fetch_pages_slate_bucket(league: str) -> dict[str, list[dict[str, Any]]] | None:
    now = time.monotonic()
    cached = _SLATE_CACHE.get(league)
    if cached and now - cached[0] < _SLATE_TTL_S:
        return cached[1]

    payload = await fetch_pages_json(f"{league}/slate.json")
    if not payload:
        return None
    bucket = {
        "today": list(payload.get("todayGames") or []),
        "tomorrow": list(payload.get("tomorrowGames") or []),
    }
    _SLATE_CACHE[league] = (now, bucket)
    return bucket


async def seed_matchup_from_pages(
    league: str,
    team_id: int,
    games: int,
    *,
    store,
    cache_version: int,
) -> dict[str, Any] | None:
    """Copy Pages snapshot into memory cache."""
    data = await fetch_pages_matchup(league, team_id, games)
    if not data:
        return None
    data["cacheVersion"] = cache_version
    await store(team_id, games, data)
    logger.info("Seeded %s team %s from GitHub Pages", league, team_id)
    return data


async def warm_all_matchups_from_pages(
    leagues: list[tuple[str, list[int], Callable[[int, int], Any], Any, int]],
    *,
    games: int = 10,
    concurrency: int = 8,
) -> dict[str, int]:
    """Preload every team from GitHub Pages (parallel). Run on boot + periodic sync."""
    sem = asyncio.Semaphore(concurrency)
    stats = {"seeded": 0, "skipped": 0, "failed": 0}

    async def one(
        league: str,
        team_id: int,
        get_fn: Callable[[int, int], Any],
        store_fn: Any,
        cache_version: int,
    ) -> None:
        async with sem:
            try:
                if get_fn(team_id, games):
                    stats["skipped"] += 1
                    return
                seeded = await seed_matchup_from_pages(
                    league,
                    team_id,
                    games,
                    store=store_fn,
                    cache_version=cache_version,
                )
                if seeded:
                    stats["seeded"] += 1
                else:
                    stats["failed"] += 1
            except Exception:
                stats["failed"] += 1
                logger.exception("Pages warm failed for %s team %s", league, team_id)

    await asyncio.gather(
        *[
            one(league, team_id, get_fn, store_fn, cache_version)
            for league, team_ids, get_fn, store_fn, cache_version in leagues
            for team_id in team_ids
        ]
    )
    logger.info("Pages warm complete: %s", stats)
    return stats


def cloud_lite_league_specs() -> list[tuple[str, list[int], Callable, Any, int]]:
    from app.cache import CACHE_VERSION as MLB_V, get_matchup as get_mlb, store_matchup as store_mlb
    from app.cpbl_cache import CACHE_VERSION as CPBL_V, get_matchup as get_cpbl, store_matchup as store_cpbl
    from app.npb_cache import CACHE_VERSION as NPB_V, get_matchup as get_npb, store_matchup as store_npb

    return [
        ("mlb", list(range(108, 158)), get_mlb, store_mlb, MLB_V),
        ("npb", list(range(1, 13)), get_npb, store_npb, NPB_V),
        ("cpbl", list(range(1, 7)), get_cpbl, store_cpbl, CPBL_V),
    ]


async def warm_pages_mirror_background() -> dict[str, int]:
    """Background-friendly alias used by boot + periodic sync."""
    from app.mlb_service import fetch_teams

    specs = cloud_lite_league_specs()
    mlb_ids = [int(team["id"]) for team in await fetch_teams()]
    specs[0] = ("mlb", mlb_ids, specs[0][2], specs[0][3], specs[0][4])
    return await warm_all_matchups_from_pages(specs)
