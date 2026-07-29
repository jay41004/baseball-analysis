"""Batter RISP totals from stats.cpbl play-by-play."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.cpbl_season_batting import format_avg
from app.cpbl_stats import STATS_HEADERS, _fetch_stats_game_html, parse_stats_pbp_plays
from app.cpbl_vs_pitcher import _NON_AB, _dedupe_plays, _is_hit, _terminal_action

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_TTL = timedelta(hours=12)
_BUILD_LOCK = asyncio.Lock()
_BUILD_TASK: asyncio.Task[None] | None = None


def _cache_file(year: int) -> Path:
    return BASE_DIR / "data" / f"cpbl_risp_batting_{year}.json"


def _empty_bucket() -> dict[str, int]:
    return {"ab": 0, "h": 0}


def _is_risp(play: dict[str, Any]) -> bool:
    second = str(play.get("secondBase") or "").strip()
    third = str(play.get("thirdBase") or "").strip()
    return bool(second or third)


def _extract_risp_pa_outcomes(plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for play in _dedupe_plays(plays):
        hitter = str(play.get("hitterAcnt") or "")
        if not hitter:
            continue
        pa_key = (
            play.get("year") or "",
            play.get("gameSno") or "",
            play.get("inningSeq"),
            hitter,
            play.get("battingOrder"),
        )
        grouped[pa_key].append(play)

    outcomes: list[dict[str, Any]] = []
    for pa_key, rows in grouped.items():
        rows.sort(key=lambda row: int(row.get("pitchCnt") or 0))
        if not _is_risp(rows[0]):
            continue
        action = ""
        for row in reversed(rows):
            candidate = (row.get("battingActionName") or row.get("actionName") or "").strip()
            if _terminal_action(candidate):
                action = candidate
                break
        if not action or action in _NON_AB:
            continue
        outcomes.append({"hitterAcnt": pa_key[3], "action": action})
    return outcomes


def _merge_outcome(bucket: dict[str, int], action: str) -> None:
    bucket["ab"] += 1
    if _is_hit(action):
        bucket["h"] += 1


def lookup_risp_avg(players: dict[str, Any], hitter_acnt: str) -> str | None:
    bucket = (players or {}).get(hitter_acnt) or {}
    ab = int(bucket.get("ab") or 0)
    if ab <= 0:
        return None
    hits = int(bucket.get("h") or 0)
    return format_avg(hits / ab)


async def build_risp_index(year: int | None = None) -> dict[str, Any]:
    year = year or date.today().year
    from app.cpbl_stats import fetch_stats_schedule

    players: dict[str, dict[str, int]] = {}
    async with httpx.AsyncClient(timeout=60, headers=STATS_HEADERS) as http:
        schedule = await fetch_stats_schedule(http)
        games = [
            game
            for game in schedule
            if game.get("gameSno") is not None
            and int(game.get("year") or year) == year
            and game.get("status") in {"Final", "In Progress", "Live", "Scheduled"}
        ]
        games.sort(key=lambda game: game.get("gameSno") or 0)

        for game in games:
            game_sno = int(game["gameSno"])
            game_year = int(game.get("year") or year)
            html = await _fetch_stats_game_html(http, game_sno, game_year)
            if not html:
                await asyncio.sleep(0.15)
                continue
            plays = parse_stats_pbp_plays(html, game_sno=game_sno, year=game_year)
            for row in _extract_risp_pa_outcomes(plays):
                bucket = players.setdefault(str(row["hitterAcnt"]), _empty_bucket())
                _merge_outcome(bucket, row["action"])
            await asyncio.sleep(0.08)

    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "year": year,
        "players": players,
    }
    _save_disk_cache(payload)
    return payload


def _save_disk_cache(payload: dict[str, Any]) -> None:
    path = _cache_file(int(payload.get("year") or date.today().year))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("Failed to write CPBL RISP cache")


def _load_disk_cache(year: int, *, allow_stale: bool = True) -> dict[str, Any] | None:
    path = _cache_file(year)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(raw["updatedAt"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if not allow_stale and datetime.now(timezone.utc) - updated > CACHE_TTL:
            return None
        players = raw.get("players")
        if isinstance(players, dict):
            return raw
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None


async def get_risp_lookup(year: int | None = None) -> dict[str, Any]:
    year = year or date.today().year
    cached = _load_disk_cache(year)
    if cached:
        return cached.get("players") or {}
    return {}


def schedule_risp_refresh(year: int | None = None) -> None:
    global _BUILD_TASK

    async def _run() -> None:
        async with _BUILD_LOCK:
            try:
                data = await build_risp_index(year)
                logger.info(
                    "Built CPBL RISP cache for %s (%s players)",
                    data.get("year"),
                    len(data.get("players") or {}),
                )
            except Exception:
                logger.exception("Failed to build CPBL RISP cache")

    if _BUILD_TASK and not _BUILD_TASK.done():
        return
    _BUILD_TASK = asyncio.create_task(_run())
