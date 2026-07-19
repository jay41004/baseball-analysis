"""In-memory + disk cache for MLB analysis results."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.team_names import localize_analysis

CACHE_TTL = timedelta(hours=1)
DEFAULT_GAMES = 10
CACHE_VERSION = 10

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "data" / "cache.json"

_lock = asyncio.Lock()
_store: dict[str, dict[str, Any]] = {}


def _cache_key(team_id: int, games: int) -> str:
    return f"{team_id}:{games}"


def _matchup_key(team_id: int, games: int) -> str:
    return f"matchup:v{CACHE_VERSION}:{team_id}:{games}"


def get_matchup(team_id: int, games: int) -> dict[str, Any] | None:
    return _store.get(_matchup_key(team_id, games))


def cached_team_count(games: int = DEFAULT_GAMES) -> int:
    prefix = f"matchup:v{CACHE_VERSION}:"
    suffix = f":{games}"
    return sum(1 for key in _store if key.startswith(prefix) and key.endswith(suffix))


async def store_matchup(team_id: int, games: int, data: dict[str, Any]) -> dict[str, Any]:
    entry = {"data": data, "updatedAt": _now_iso()}
    async with _lock:
        _store[_matchup_key(team_id, games)] = entry
        save_to_disk()
    return entry


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def is_stale(updated_at: str) -> bool:
    return datetime.now(timezone.utc).astimezone() - _parse_time(updated_at) > CACHE_TTL


def load_from_disk() -> None:
    if not CACHE_FILE.exists():
        return
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _store.update(raw)
            _migrate_cache_keys()
    except (json.JSONDecodeError, OSError):
        pass


def _migrate_cache_keys() -> None:
    """Drop legacy matchup entries so stats are rebuilt with current logic."""
    stale_keys = [
        key for key in _store if key.startswith("matchup:") and not key.startswith(f"matchup:v{CACHE_VERSION}:")
    ]
    for key in stale_keys:
        _store.pop(key, None)
    if stale_keys:
        save_to_disk()


def save_to_disk() -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(_store, ensure_ascii=False, indent=2), encoding="utf-8")


def get(team_id: int, games: int) -> dict[str, Any] | None:
    return _store.get(_cache_key(team_id, games))


async def store(team_id: int, games: int, data: dict[str, Any]) -> dict[str, Any]:
    entry = {"data": data, "updatedAt": _now_iso()}
    async with _lock:
        _store[_cache_key(team_id, games)] = entry
        save_to_disk()
    return entry


def localize_matchup(data: dict) -> dict:
    data = dict(data)
    matchup = data.get("matchup")
    if matchup:
        data["matchup"] = dict(matchup)

    for side in ("away", "home"):
        panel = data.get(side)
        if panel:
            data[side] = localize_analysis(dict(panel))

    return data


def wrap_matchup_response(
    entry: dict[str, Any], *, refreshing: bool = False, from_cache: bool = True
) -> dict[str, Any]:
    updated_at = entry["updatedAt"]
    next_refresh = _parse_time(updated_at) + CACHE_TTL
    data = localize_matchup(dict(entry["data"]))
    return {
        **data,
        "cacheVersion": CACHE_VERSION,
        "cachedAt": updated_at,
        "nextRefreshAt": next_refresh.isoformat(timespec="seconds"),
        "fromCache": from_cache,
        "refreshing": refreshing,
    }


def wrap_response(
    entry: dict[str, Any], *, refreshing: bool = False, from_cache: bool = True
) -> dict[str, Any]:
    updated_at = entry["updatedAt"]
    next_refresh = _parse_time(updated_at) + CACHE_TTL
    data = localize_analysis(dict(entry["data"]))
    return {
        **data,
        "cachedAt": updated_at,
        "nextRefreshAt": next_refresh.isoformat(timespec="seconds"),
        "fromCache": from_cache,
        "refreshing": refreshing,
    }
