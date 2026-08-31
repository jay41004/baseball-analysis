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


async def refresh_matchup_header(team_id: int, games: int = DEFAULT_GAMES) -> None:
    """Update next-game header; rebuild pitcher blocks when starter missing/changed."""
    import copy

    import httpx

    from app.mlb_service import fetch_next_matchup, rebuild_pitcher_dependent_fields
    from app.pages_mirror import seed_matchup_from_pages

    cached = get_matchup(team_id, games)
    if not cached:
        await seed_matchup_from_pages(
            "mlb", team_id, games, store=store_matchup, cache_version=CACHE_VERSION
        )
        cached = get_matchup(team_id, games)
    if not cached:
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        matchup = await fetch_next_matchup(client, team_id)
    if not matchup:
        return

    data = copy.deepcopy(cached["data"])
    old_away = int((data.get("away") or {}).get("teamId") or 0)
    old_home = int((data.get("home") or {}).get("teamId") or 0)
    new_away = int(matchup["away"]["teamId"])
    new_home = int(matchup["home"]["teamId"])
    old_pk = (data.get("matchup") or {}).get("gamePk")
    new_pk = matchup.get("gamePk")
    game_changed = old_pk is not None and new_pk is not None and old_pk != new_pk

    def _name(panel: dict | None) -> str:
        return ((panel or {}).get("probablePitcher") or {}).get("fullName") or ""

    data["matchup"] = {
        "date": matchup.get("date"),
        "gameDate": matchup.get("gameDate"),
        "gamePk": matchup.get("gamePk"),
        "status": matchup.get("status"),
        "stadium": matchup.get("stadium"),
        "timeTaiwan": matchup.get("timeTaiwan"),
        "timeLocal": matchup.get("timeLocal"),
    }

    if {old_away, old_home} == {new_away, new_home}:
        if old_away == new_home and old_home == new_away:
            data["away"], data["home"] = data["home"], data["away"]
        for side in ("away", "home"):
            panel = data.get(side) or {}
            src = matchup[side]
            panel["teamId"] = src["teamId"]
            panel["teamName"] = src["teamName"]
            new_pitcher = src.get("probablePitcher")
            new_name = (new_pitcher or {}).get("fullName") or ""
            old_name = _name(panel)
            if game_changed:
                panel["probablePitcher"] = new_pitcher
                if old_name != new_name:
                    panel.pop("pitcherAnalysis", None)
            elif new_pitcher and new_name:
                panel["probablePitcher"] = new_pitcher
                if old_name and new_name and old_name != new_name:
                    panel.pop("pitcherAnalysis", None)
            data[side] = panel
    else:
        # Opponents changed — keep correct header; drop stale panels.
        empty_summary = {
            "totalGames": 0,
            "over15": 0,
            "under15": 0,
            "over25": 0,
            "under25": 0,
            "avgRuns": 0,
            "avgFirstFive": 0,
            "firstInningScored": 0,
        }
        for side in ("away", "home"):
            src = matchup[side]
            data[side] = {
                "teamId": src["teamId"],
                "teamName": src["teamName"],
                "probablePitcher": src.get("probablePitcher"),
                "games": [],
                "summary": dict(empty_summary),
            }
        data["startingLineups"] = {"away": {"batters": []}, "home": {"batters": []}}
        data.pop("aTable", None)
        data.pop("situational", None)

    from app.pitcher_rows import pitcher_analysis_missing_pitch_counts

    has_starter = any(_name(data.get(side)) for side in ("away", "home"))
    needs_analysis = pitcher_analysis_missing_pitch_counts(data) or any(
        _name(data.get(side))
        and not ((data.get(side) or {}).get("pitcherAnalysis") or {}).get("games")
        for side in ("away", "home")
    )

    if needs_analysis and has_starter:
        try:
            data = await rebuild_pitcher_dependent_fields(data, game_count=games)
        except Exception:
            logger.exception(
                "MLB pitcher rebuild failed for team %s (header kept)", team_id
            )

    data["cacheVersion"] = CACHE_VERSION
    await store_matchup(team_id, games, data)
    logger.info("Refreshed MLB matchup header for team %s", team_id)


async def refresh_matchup(team_id: int, games: int = DEFAULT_GAMES) -> None:
    key = f"matchup:v{CACHE_VERSION}:{team_id}:{games}"
    if key in _refreshing_keys:
        return

    _refreshing_keys.add(key)
    try:
        from app.cloud_lite import is_cloud_lite

        if is_cloud_lite():
            # Free tier: never rebuild full panels in-process (OOM → 502).
            await refresh_matchup_header(team_id, games)
        else:
            data = await analyze_matchup(team_id, games, lite=False)
            data["cacheVersion"] = CACHE_VERSION
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
            else:
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
            try:
                from app.data_validate import validate_mlb_cache

                report = await validate_mlb_cache(games=games)
                if not report.get("ok"):
                    logger.error(
                        "MLB validation issues (%s): %s",
                        len(report.get("issues") or []),
                        (report.get("issues") or [])[:8],
                    )
            except Exception:
                logger.exception("MLB post-refresh validation failed")
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