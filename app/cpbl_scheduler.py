"""Background refresh of cached CPBL analysis."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from typing import Any

from app.cpbl_cache import (
    CACHE_VERSION,
    DEFAULT_GAMES,
    cached_team_count,
    get_a_table,
    get_matchup,
    is_stale,
    load_from_disk,
    store_a_table,
    store_matchup,
)
from app.cpbl_season_batting import schedule_season_batting_refresh
from app.cpbl_risp_batting import schedule_risp_refresh
from app.cpbl_vs_pitcher import schedule_vs_pitcher_refresh
from app.cpbl_service import (
    CpblClient,
    analyze_matchup,
    analyze_matchup_a_table,
    fetch_cpbl_teams,
    fetch_matchup_starting_lineups,
    fetch_next_matchup,
)

logger = logging.getLogger(__name__)

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3600"))
WARMUP_CONCURRENCY = int(os.environ.get("CPBL_WARMUP_CONCURRENCY", "2"))

_refresh_lock = asyncio.Lock()
_refreshing_keys: set[str] = set()
_warming_all = False


def is_refreshing(team_id: int, games: int = DEFAULT_GAMES) -> bool:
    key = f"cpbl:matchup:v{CACHE_VERSION}:{team_id}:{games}"
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
        logger.info("Refreshed CPBL a-table cache for team %s", team_id)
    except Exception:
        logger.exception("Failed to refresh CPBL a-table for team %s", team_id)
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
        raise ValueError(f"無法產生 CPBL a 表格（team {team_id}）")
    return cached


async def refresh_matchup_lineups(team_id: int, games: int = DEFAULT_GAMES) -> None:
    cached = get_matchup(team_id, games)
    if not cached:
        return
    data = copy.deepcopy(cached["data"])
    client = CpblClient()
    try:
        matchup = await fetch_next_matchup(client, team_id)
        if not matchup:
            return
        data["startingLineups"] = await fetch_matchup_starting_lineups(client, matchup)
    except Exception:
        logger.exception("Failed to refresh CPBL lineups for team %s", team_id)
        return
    finally:
        await client.close()
    await store_matchup(team_id, games, data)


async def refresh_matchup_header(team_id: int, games: int = DEFAULT_GAMES) -> None:
    """Cloud-safe: mirror GitHub Pages snapshot (no live CPBL crawl on free tier)."""
    from app.cloud_lite import is_cloud_lite
    from app.loading_response import loading_matchup_payload
    from app.pages_mirror import seed_matchup_from_pages

    # Free tier OOMs if we rebuild schedule/panels — Pages is the data source.
    if is_cloud_lite():
        await seed_matchup_from_pages(
            "cpbl", team_id, games, store=store_matchup, cache_version=CACHE_VERSION
        )
        return

    cached = get_matchup(team_id, games)
    if not cached:
        await seed_matchup_from_pages(
            "cpbl", team_id, games, store=store_matchup, cache_version=CACHE_VERSION
        )
        cached = get_matchup(team_id, games)

    matchup = None
    client = CpblClient()
    try:
        matchup = await fetch_next_matchup(client, team_id)
    except Exception:
        logger.exception("CPBL header fetch failed for team %s (keeping Pages/cache)", team_id)
    finally:
        await client.close()
    if not matchup:
        return

    if cached:
        data = copy.deepcopy(cached["data"])
    else:
        data = loading_matchup_payload(team_id, cache_version=CACHE_VERSION)
        data.pop("loading", None)
        data.pop("refreshing", None)

    old_away = int((data.get("away") or {}).get("teamId") or 0)
    old_home = int((data.get("home") or {}).get("teamId") or 0)
    new_away = int(matchup["away"]["teamId"])
    new_home = int(matchup["home"]["teamId"])

    data["cacheVersion"] = CACHE_VERSION
    data["focusTeamId"] = team_id
    data["matchup"] = {
        "date": matchup.get("date"),
        "gameDate": matchup.get("gameDate"),
        "gameSno": matchup.get("gameSno"),
        "status": matchup.get("status"),
        "stadium": matchup.get("stadium"),
    }

    if old_away and old_home and {old_away, old_home} == {new_away, new_home}:
        if old_away == new_home and old_home == new_away:
            data["away"], data["home"] = data["home"], data["away"]
        for side in ("away", "home"):
            panel = data.get(side) or {}
            src = matchup[side]
            panel["teamId"] = src["teamId"]
            panel["teamName"] = src.get("teamName") or panel.get("teamName")
            if src.get("probablePitcher"):
                panel["probablePitcher"] = src["probablePitcher"]
            data[side] = panel
    else:
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
                "teamName": src.get("teamName") or "",
                "probablePitcher": src.get("probablePitcher"),
                "games": [],
                "summary": dict(empty_summary),
            }
        data["startingLineups"] = {"away": {"batters": []}, "home": {"batters": []}}
        data.pop("aTable", None)
        data.pop("situational", None)

    await store_matchup(team_id, games, data)
    logger.info("Refreshed CPBL matchup header for team %s → %s", team_id, matchup.get("date"))


async def refresh_matchup(team_id: int, games: int = DEFAULT_GAMES) -> None:
    key = f"cpbl:matchup:v{CACHE_VERSION}:{team_id}:{games}"
    if key in _refreshing_keys:
        return

    _refreshing_keys.add(key)
    try:
        from app.cloud_lite import is_cloud_lite

        if is_cloud_lite():
            await refresh_matchup_header(team_id, games)
            return

        from app.cpbl_service import invalidate_shared_schedule_cache

        # Clear in-memory schedule only so status repair / pitchers re-apply.
        # Do not wipe the disk schedule file on every team refresh.
        invalidate_shared_schedule_cache(wipe_disk=False)
        data = await analyze_matchup(team_id, games)
        client = CpblClient()
        try:
            matchup = await fetch_next_matchup(client, team_id)
            if matchup:
                data["startingLineups"] = await fetch_matchup_starting_lineups(client, matchup)
        except Exception:
            logger.exception("Failed to fetch CPBL lineups during refresh for team %s", team_id)
        finally:
            await client.close()
        await store_matchup(team_id, games, data)
        if a_table := data.get("aTable"):
            await store_a_table(team_id, a_table)
        logger.info("Refreshed CPBL matchup cache for team %s (%s games)", team_id, games)
    except Exception:
        logger.exception("Failed to refresh CPBL matchup for team %s", team_id)
    finally:
        _refreshing_keys.discard(key)


def _teams_needing_refresh(teams: list[dict], games: int) -> list[dict]:
    stale: list[dict] = []
    for team in teams:
        entry = get_matchup(team["id"], games)
        if entry is None or is_stale(entry["updatedAt"]):
            stale.append(team)
            continue
        from app.cpbl_cache import cache_needs_upgrade

        if cache_needs_upgrade(entry):
            stale.append(team)
    return stale


async def refresh_all_matchups(games: int = DEFAULT_GAMES) -> None:
    global _warming_all
    async with _refresh_lock:
        _warming_all = True
        try:
            teams = await fetch_cpbl_teams()
            targets = _teams_needing_refresh(teams, games)
            if not targets:
                logger.info("CPBL cache already warm for all %s teams", len(teams))
                return

            semaphore = asyncio.Semaphore(WARMUP_CONCURRENCY)

            async def refresh_one(team: dict) -> None:
                async with semaphore:
                    await refresh_matchup(team["id"], games)

            await asyncio.gather(*[refresh_one(team) for team in targets])
            logger.info(
                "Finished warming CPBL cache (%s/%s teams refreshed)",
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


async def warm_schedule_pool() -> None:
    from app.cpbl_service import CpblClient

    client = CpblClient()
    try:
        games = await client.fetch_schedule_pool()
        logger.info("CPBL schedule pool warmed (%s games)", len(games))
    except Exception:
        logger.exception("CPBL schedule warm-up failed")
    finally:
        await client.close()


async def bootstrap_cpbl_cache() -> None:
    from app.cpbl_service import SCHEDULE_CACHE_FILE

    if SCHEDULE_CACHE_FILE.exists():
        logger.info("CPBL schedule on disk; skipping network warm")
    else:
        await warm_schedule_pool()
    schedule_season_batting_refresh()
    schedule_vs_pitcher_refresh()
    schedule_risp_refresh()
    asyncio.create_task(_deferred_refresh_all())


async def _deferred_refresh_all() -> None:
    delay = int(os.environ.get("CPBL_DEFERRED_REFRESH_SECONDS", "45"))
    await asyncio.sleep(delay)
    await refresh_all_matchups(DEFAULT_GAMES)


async def start_cpbl_cache_services(*, skip_load: bool = False) -> None:
    if not skip_load:
        load_from_disk()
    logger.info(
        "CPBL cache loaded (%s teams on disk).",
        cached_team_count(DEFAULT_GAMES),
    )
    from app.cloud_lite import is_cloud_lite

    if not is_cloud_lite():
        asyncio.create_task(bootstrap_cpbl_cache())
        asyncio.create_task(hourly_refresh_loop())
