"""Cache for CPBL analysis (separate from MLB/NPB)."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.inning_comparison import a_table_payload_complete

CACHE_TTL = timedelta(hours=1)
DEFAULT_GAMES = 10
CACHE_VERSION = 17

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "data" / "cpbl_cache.json"

_lock = asyncio.Lock()
_store: dict[str, dict[str, Any]] = {}


def _key_prefix() -> str:
    return f"cpbl:matchup:v{CACHE_VERSION}:"


def _matchup_key(team_id: int, games: int) -> str:
    return f"cpbl:matchup:v{CACHE_VERSION}:{team_id}:{games}"


ATABLE_REVISION = 2


def _a_table_key(team_id: int) -> str:
    return f"cpbl:atable:v{CACHE_VERSION}:r{ATABLE_REVISION}:{team_id}"


def get_matchup(team_id: int, games: int) -> dict[str, Any] | None:
    hit = _store.get(_matchup_key(team_id, games))
    if hit:
        return hit
    # Prefer newest cacheVersion among any loaded keys for this team/games.
    suffix = f":{team_id}:{games}"
    best: dict[str, Any] | None = None
    best_ver = -1
    for key, value in _store.items():
        if not (key.startswith("cpbl:matchup:v") and key.endswith(suffix)):
            continue
        try:
            ver = int(key.split("cpbl:matchup:v", 1)[1].split(":", 1)[0])
        except (TypeError, ValueError, IndexError):
            ver = 0
        if ver > best_ver:
            best_ver = ver
            best = value
    return best


def cache_needs_upgrade(entry: dict[str, Any]) -> bool:
    data = entry.get("data") or {}
    if int(data.get("cacheVersion") or 0) < CACHE_VERSION:
        return True
    situational = data.get("situational") or {}
    away_pool = (situational.get("awayTeamAwayGames") or {}).get("gameCount") or 0
    home_pool = (situational.get("homeTeamHomeGames") or {}).get("gameCount") or 0
    away_games = len((data.get("away") or {}).get("games") or [])
    home_games = len((data.get("home") or {}).get("games") or [])
    if away_games > 0 and away_pool == 0:
        return True
    if home_games > 0 and home_pool == 0:
        return True
    lineups = data.get("startingLineups") or {}
    away_batters = len((lineups.get("away") or {}).get("batters") or [])
    home_batters = len((lineups.get("home") or {}).get("batters") or [])
    # Incomplete official firstSno (often 6–7) should be rebuilt to a full 9-man card.
    if (away_batters and away_batters < 9) or (home_batters and home_batters < 9):
        return True
    from app.pitcher_rows import pitcher_analysis_missing_pitch_counts

    return pitcher_analysis_missing_pitch_counts(data)


def get_a_table(team_id: int) -> dict[str, Any] | None:
    entry = _store.get(_a_table_key(team_id))
    if not entry:
        return None
    data = entry.get("data")
    if not data or not a_table_payload_complete(data):
        return None
    return entry


async def store_a_table(team_id: int, data: dict[str, Any]) -> dict[str, Any]:
    entry = {"data": data, "updatedAt": _now_iso()}
    async with _lock:
        _store[_a_table_key(team_id)] = entry
        save_to_disk()
    return entry


def wrap_a_table_response(
    entry: dict[str, Any], *, refreshing: bool = False, from_cache: bool = True
) -> dict[str, Any]:
    updated_at = entry["updatedAt"]
    next_refresh = _parse_time(updated_at) + CACHE_TTL
    return {
        **copy.deepcopy(entry["data"]),
        "cacheVersion": CACHE_VERSION,
        "cachedAt": updated_at,
        "nextRefreshAt": next_refresh.isoformat(timespec="seconds"),
        "fromCache": from_cache,
        "refreshing": refreshing,
    }


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
        loaded = {
            key: value
            for key, value in raw.items()
            if key.startswith("cpbl:matchup:v") or key.startswith("cpbl:atable:v")
        }
        _store.update(loaded)
    except (json.JSONDecodeError, OSError):
        pass


def save_to_disk() -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if CACHE_FILE.exists():
        try:
            raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
        except (json.JSONDecodeError, OSError):
            existing = {}
    # Merge in-memory entries (do not wipe other processes' / older-version keys).
    existing.update(_store)
    # Drop stale older matchup versions for the same team/games when current exists.
    current_prefix = _key_prefix()
    drop: list[str] = []
    for key in list(existing):
        if not key.startswith("cpbl:matchup:v") or key.startswith(current_prefix):
            continue
        suffix = key.split("cpbl:matchup:v", 1)[-1]
        # suffix like "14:1:10" → team:games is after first ':'
        parts = suffix.split(":")
        if len(parts) < 3:
            continue
        team_games = ":".join(parts[1:])
        if f"{current_prefix}{team_games}" in existing:
            drop.append(key)
    for key in drop:
        existing.pop(key, None)
    CACHE_FILE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def wrap_matchup_response(
    entry: dict[str, Any], *, refreshing: bool = False, from_cache: bool = True
) -> dict[str, Any]:
    updated_at = entry["updatedAt"]
    next_refresh = _parse_time(updated_at) + CACHE_TTL
    return {
        **copy.deepcopy(entry["data"]),
        "cacheVersion": CACHE_VERSION,
        "cachedAt": updated_at,
        "nextRefreshAt": next_refresh.isoformat(timespec="seconds"),
        "fromCache": from_cache,
        "refreshing": refreshing,
    }
