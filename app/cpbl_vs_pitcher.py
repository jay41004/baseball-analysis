"""Batter vs pitcher totals from stats.cpbl play-by-play."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.cpbl_stats import (
    STATS_HEADERS,
    _fetch_stats_game_html,
    parse_stats_pbp_plays,
)
from app.cpbl_season_batting import format_avg

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_TTL = timedelta(hours=12)
_BUILD_LOCK = asyncio.Lock()
_BUILD_TASK: asyncio.Task[None] | None = None

_NON_AB = frozenset({"四壞", "故意四壞", "死球", "犧飛", "犧觸", "保送"})
_HIT = frozenset({"一安", "二安", "三安", "全壘打", "內野安打", "場內全壘打"})


def _cache_file(year: int) -> Path:
    return BASE_DIR / "data" / f"cpbl_vs_pitcher_{year}.json"


def _pair_key(hitter_acnt: str, pitcher_acnt: str) -> str:
    return f"{hitter_acnt}|{pitcher_acnt}"


def _empty_bucket() -> dict[str, int]:
    return {"ab": 0, "h": 0}


def _terminal_action(action: str) -> bool:
    if not action or action in _NON_AB:
        return False
    if action in _HIT:
        return True
    if "安" in action and action.endswith("安"):
        return True
    if any(token in action for token in ("滾", "飛", "振", "殺")):
        return True
    return False


def _is_hit(action: str) -> bool:
    if action in _HIT:
        return True
    return "安" in action and "失" not in action and action != "三振"


def _dedupe_plays(plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    rows: list[dict[str, Any]] = []
    for play in plays:
        key = (
            play.get("inningSeq"),
            play.get("mainEventNo"),
            play.get("pitchCnt"),
            play.get("hitterAcnt"),
            play.get("pitcherAcnt"),
            play.get("battingActionName"),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(play)
    return rows


def _extract_pa_outcomes(plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicated PBP rows into plate-appearance outcomes."""
    grouped: dict[tuple[Any, ...], str] = {}
    for play in _dedupe_plays(plays):
        hitter = str(play.get("hitterAcnt") or "")
        pitcher = str(play.get("pitcherAcnt") or "")
        if not hitter or not pitcher:
            continue
        action = (play.get("battingActionName") or play.get("actionName") or "").strip()
        if not _terminal_action(action):
            continue
        pa_key = (
            play.get("year") or "",
            play.get("gameSno") or "",
            play.get("inningSeq"),
            hitter,
            pitcher,
            action,
            play.get("battingOrder"),
        )
        grouped[pa_key] = action
    outcomes: list[dict[str, Any]] = []
    for key, action in grouped.items():
        year, game_sno, inning, hitter, pitcher, _, _order = key
        outcomes.append(
            {
                "year": str(year),
                "hitterAcnt": hitter,
                "pitcherAcnt": pitcher,
                "action": action,
            }
        )
    return outcomes


def _merge_outcome(bucket: dict[str, int], action: str) -> None:
    bucket["ab"] += 1
    if _is_hit(action):
        bucket["h"] += 1


def _finalize_avg(bucket: dict[str, int]) -> str | None:
    ab = int(bucket.get("ab") or 0)
    if ab <= 0:
        return None
    hits = int(bucket.get("h") or 0)
    return format_avg(hits / ab)


def parse_pitcher_acnt_map(html: str, *, game_sno: int, year: int) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for play in parse_stats_pbp_plays(html, game_sno=game_sno, year=year):
        name = (play.get("pitcherName") or "").strip()
        acnt = str(play.get("pitcherAcnt") or "")
        if name and acnt:
            mapping[name] = acnt
    return mapping


async def build_vs_pitcher_index(year: int | None = None) -> dict[str, Any]:
    year = year or date.today().year
    from app.cpbl_stats import fetch_stats_schedule

    pairs: dict[str, dict[str, dict[str, int]]] = {}
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
            for row in _extract_pa_outcomes(plays):
                key = _pair_key(row["hitterAcnt"], row["pitcherAcnt"])
                entry = pairs.setdefault(
                    key,
                    {"season": _empty_bucket(), "career": _empty_bucket()},
                )
                _merge_outcome(entry["career"], row["action"])
                if str(row.get("year") or game_year) == str(year):
                    _merge_outcome(entry["season"], row["action"])
            await asyncio.sleep(0.08)

    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "year": year,
        "pairs": pairs,
    }
    _save_disk_cache(payload)
    return payload


def _save_disk_cache(payload: dict[str, Any]) -> None:
    path = _cache_file(int(payload.get("year") or date.today().year))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("Failed to write CPBL vs-pitcher cache")


def _load_disk_cache(year: int, *, allow_stale: bool = True) -> dict[str, Any] | None:
    path = _cache_file(year)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(raw["updatedAt"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if not allow_stale and datetime.now(timezone.utc) - updated > CACHE_TTL:
            return None
        pairs = raw.get("pairs")
        if isinstance(pairs, dict):
            return raw
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None


async def get_vs_pitcher_lookup(year: int | None = None) -> dict[str, Any]:
    year = year or date.today().year
    cached = _load_disk_cache(year)
    if cached:
        return cached.get("pairs") or {}
    return {}


def schedule_vs_pitcher_refresh(year: int | None = None) -> None:
    global _BUILD_TASK
    year = year or date.today().year

    async def _run() -> None:
        async with _BUILD_LOCK:
            try:
                await build_vs_pitcher_index(year)
            except Exception:
                logger.exception("CPBL vs-pitcher refresh failed")

    if _BUILD_TASK and not _BUILD_TASK.done():
        return
    _BUILD_TASK = asyncio.create_task(_run())


def lookup_vs_pitcher_avgs(
    pairs: dict[str, Any],
    *,
    hitter_acnt: str,
    pitcher_acnt: str,
) -> dict[str, str | None]:
    entry = pairs.get(_pair_key(hitter_acnt, pitcher_acnt)) or {}
    season = entry.get("season") or {}
    career = entry.get("career") or {}
    return {
        "vsPitcherSeasonAvg": _finalize_avg(season),
        "vsPitcherCareerAvg": _finalize_avg(career),
    }
