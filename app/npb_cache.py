"""Cache for NPB analysis (separate from MLB)."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.npb_display import localize_matchup_payload

CACHE_TTL = timedelta(hours=1)
DEFAULT_GAMES = 10
CACHE_VERSION = 14

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "data" / "npb_cache.json"

_lock = asyncio.Lock()
_store: dict[str, dict[str, Any]] = {}


def _key_prefix() -> str:
    return f"npb:matchup:v{CACHE_VERSION}:"


def _matchup_key(team_id: int, games: int) -> str:
    return f"npb:matchup:v{CACHE_VERSION}:{team_id}:{games}"


def get_matchup(team_id: int, games: int) -> dict[str, Any] | None:
    return _store.get(_matchup_key(team_id, games))


def cached_team_count(games: int = DEFAULT_GAMES) -> int:
    prefix = _key_prefix()
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
        if not isinstance(raw, dict):
            return
        prefix = _key_prefix()
        legacy_prefix = f"npb:matchup:v{CACHE_VERSION - 1}:"
        current: dict[str, dict[str, Any]] = {}
        migrated = False
        for key, value in raw.items():
            if key.startswith(prefix):
                current[key] = value
            elif key.startswith(legacy_prefix):
                current[f"{prefix}{key[len(legacy_prefix):]}"] = value
                migrated = True
        _store.update(current)
        if migrated or len(current) != len(raw):
            save_to_disk()
    except (json.JSONDecodeError, OSError):
        pass


def _migrate_cache_keys() -> None:
    prefix = _key_prefix()
    stale = [key for key in _store if key.startswith("npb:matchup:") and not key.startswith(prefix)]
    for key in stale:
        _store.pop(key, None)
    if stale:
        save_to_disk()


def save_to_disk() -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    prefix = _key_prefix()
    payload = {key: value for key, value in _store.items() if key.startswith(prefix)}
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def wrap_matchup_response(
    entry: dict[str, Any], *, refreshing: bool = False, from_cache: bool = True
) -> dict[str, Any]:
    updated_at = entry["updatedAt"]
    next_refresh = _parse_time(updated_at) + CACHE_TTL
    data = copy.deepcopy(entry["data"])
    localize_matchup_payload(data)
    return {
        **data,
        "cacheVersion": CACHE_VERSION,
        "cachedAt": updated_at,
        "nextRefreshAt": next_refresh.isoformat(timespec="seconds"),
        "fromCache": from_cache,
        "refreshing": refreshing,
    }
