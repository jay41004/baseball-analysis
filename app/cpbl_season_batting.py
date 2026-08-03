"""Season batting totals for CPBL (aggregated from stats.cpbl game pages)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.cpbl_stats import (
    STATS_HEADERS,
    _decode_escaped_json_array,
    _fetch_stats_game_html,
    _game_at_bats,
    _game_hit_count,
    fetch_stats_schedule,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_TTL = timedelta(hours=12)
_BUILD_LOCK = asyncio.Lock()
_BUILD_TASK: asyncio.Task[None] | None = None

HITTERS_RE = re.compile(r'\\"hitters\\":\[(.*?)\]')


def _cache_file(year: int) -> Path:
    return BASE_DIR / "data" / f"cpbl_season_batting_{year}.json"


def _empty_totals() -> dict[str, int]:
    return {
        "ab": 0,
        "h": 0,
        "hr": 0,
        "rbi": 0,
        "bb": 0,
        "hbp": 0,
        "sf": 0,
        "tb": 0,
    }


def _merge_totals(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value or 0)


def _finalize_player(totals: dict[str, int]) -> dict[str, Any]:
    ab = int(totals.get("ab") or 0)
    hits = int(totals.get("h") or 0)
    bb = int(totals.get("bb") or 0)
    hbp = int(totals.get("hbp") or 0)
    sf = int(totals.get("sf") or 0)
    tb = int(totals.get("tb") or 0)
    pa_den = ab + bb + hbp + sf
    obp = (hits + bb + hbp) / pa_den if pa_den else 0.0
    slg = tb / ab if ab else 0.0
    avg = hits / ab if ab else 0.0
    return {
        "atBats": ab,
        "hits": hits,
        "homeRuns": int(totals.get("hr") or 0),
        "rbi": int(totals.get("rbi") or 0),
        "avg": round(avg, 3),
        "ops": round(obp + slg, 3),
    }


def _parse_hitters_from_html(html: str, *, game_sno: int, year: int) -> list[dict[str, Any]]:
    marker = f"{year}-A-{game_sno}"
    idx = html.find(marker)
    if idx < 0:
        return []
    chunk = html[idx : idx + 700_000]
    rows: list[dict[str, Any]] = []
    for block in HITTERS_RE.findall(chunk)[:2]:
        if not block.strip():
            continue
        try:
            rows.extend(_decode_escaped_json_array(block))
        except json.JSONDecodeError:
            continue
    return rows


def _totals_from_hitter_row(row: dict[str, Any]) -> dict[str, int]:
    ab = _game_at_bats(row)
    hits = _game_hit_count(row)
    hr = int(row.get("homeRunCnt") or 0)
    one = int(row.get("oneBaseHitCnt") or 0)
    two = int(row.get("twoBaseHitCnt") or 0)
    three = int(row.get("threeBaseHitCnt") or 0)
    return {
        "ab": ab,
        "h": hits,
        "hr": hr,
        "rbi": int(row.get("runBattedInCnt") or 0),
        "bb": int(row.get("basesOnBallsCnt") or 0),
        "hbp": int(row.get("hitByPitchCnt") or 0),
        "sf": int(row.get("sacrificeFlyCnt") or 0),
        "tb": one + 2 * two + 3 * three + 4 * hr,
    }


def _load_disk_cache(year: int) -> dict[str, Any] | None:
    path = _cache_file(year)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(raw["updatedAt"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated > CACHE_TTL:
            return None
        players = raw.get("players")
        if isinstance(players, dict) and players:
            return raw
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None


def _save_disk_cache(year: int, players: dict[str, Any]) -> None:
    path = _cache_file(year)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                    "year": year,
                    "players": players,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to write CPBL season batting cache")


async def build_season_batting(year: int | None = None) -> dict[str, Any]:
    year = year or date.today().year
    async with httpx.AsyncClient(timeout=60, headers=STATS_HEADERS) as http:
        schedule = await fetch_stats_schedule(http)
        games = [
            game
            for game in schedule
            if game.get("gameSno") is not None
            and int(game.get("year") or year) == year
            and game.get("status") in {"Final", "In Progress", "Live"}
        ]
        games.sort(key=lambda game: game.get("gameSno") or 0)

        merged: dict[str, dict[str, int]] = {}
        for game in games:
            game_sno = int(game["gameSno"])
            html = await _fetch_stats_game_html(http, game_sno, year)
            if not html:
                await asyncio.sleep(0.25)
                continue
            for row in _parse_hitters_from_html(html, game_sno=game_sno, year=year):
                acnt = str(row.get("hitterAcnt") or "")
                if not acnt:
                    continue
                bucket = merged.setdefault(acnt, _empty_totals())
                _merge_totals(bucket, _totals_from_hitter_row(row))
            await asyncio.sleep(0.12)

    players = {acnt: _finalize_player(totals) for acnt, totals in merged.items()}
    _save_disk_cache(year, players)
    return players


async def get_season_batting_lookup(
    year: int | None = None, *, allow_stale: bool = True
) -> dict[str, dict[str, Any]]:
    year = year or date.today().year
    cached = _load_disk_cache(year)
    if cached:
        players = cached.get("players") or {}
        if isinstance(players, dict):
            return players

    if allow_stale:
        path = _cache_file(year)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            players = raw.get("players")
            if isinstance(players, dict) and players:
                return players
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    return {}


def schedule_season_batting_refresh(year: int | None = None) -> None:
    global _BUILD_TASK
    year = year or date.today().year

    async def _run() -> None:
        async with _BUILD_LOCK:
            try:
                await build_season_batting(year)
            except Exception:
                logger.exception("CPBL season batting refresh failed")

    if _BUILD_TASK and not _BUILD_TASK.done():
        return
    _BUILD_TASK = asyncio.create_task(_run())


def format_avg(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("."):
            stripped = f"0{stripped}"
        try:
            number = float(stripped)
        except (TypeError, ValueError):
            return None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
    text = f"{number:.3f}"
    return text[1:] if text.startswith("0.") else text


def format_ops(value: Any) -> str | None:
    return format_avg(value)
