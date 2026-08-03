"""Pull GitHub Pages static JSON into Render (cloud-lite sync).

GitHub Actions rebuilds docs/data every few hours. Render free tier cannot
safely rebuild full panels, so it mirrors Pages then patches next-game headers.
"""

from __future__ import annotations

import logging
import os
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


def cache_data_from_pages_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _WRAPPER_KEYS}


async def fetch_pages_matchup(
    league: str, team_id: int, games: int = 10
) -> dict[str, Any] | None:
    url = f"{PAGES_DATA_BASE}/{league}/matchup_{team_id}_{games}.json"
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "baseball-analysis-mirror/1"})
            if resp.status_code != 200:
                logger.warning("Pages miss %s → %s", url, resp.status_code)
                return None
            payload = resp.json()
            if not isinstance(payload, dict) or not payload.get("matchup"):
                return None
            return cache_data_from_pages_payload(payload)
    except Exception:
        logger.exception("Failed to fetch Pages matchup %s", url)
        return None


async def seed_matchup_from_pages(
    league: str,
    team_id: int,
    games: int,
    *,
    store,
    cache_version: int,
) -> dict[str, Any] | None:
    """If local cache empty, copy Pages snapshot (then header refresh can patch)."""
    data = await fetch_pages_matchup(league, team_id, games)
    if not data:
        return None
    data["cacheVersion"] = cache_version
    await store(team_id, games, data)
    logger.info("Seeded %s team %s from GitHub Pages", league, team_id)
    return data
