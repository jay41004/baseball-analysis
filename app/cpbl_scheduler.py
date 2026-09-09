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
from app.matchup_pick import ExpectedMatchup

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


async def refresh_matchup_header(
    team_id: int, games: int = DEFAULT_GAMES, *, expected: ExpectedMatchup | None = None
) -> None:
    """Patch next-game header (+ lineups/pitcher blocks). Seed Pages only if empty."""
    from app.cloud_lite import is_cloud_lite
    from app.loading_response import loading_matchup_payload
    from app.pages_mirror import seed_matchup_from_pages

    cached = get_matchup(team_id, games)
    # Seed Pages only when cache is empty — never wipe a richer live cache on force.
    if not cached:
        await seed_matchup_from_pages(
            "cpbl", team_id, games, store=store_matchup, cache_version=CACHE_VERSION
        )
        cached = get_matchup(team_id, games)

    matchup = None
    client = CpblClient()
    try:
        matchup = await fetch_next_matchup(client, team_id, expected=expected)
    except Exception:
        logger.exception("CPBL header fetch failed for team %s (keeping Pages/cache)", team_id)
    finally:
        await client.close()
    if not matchup:
        return

    if cached:
        data = copy.deepcopy(cached["data"])
        prev_snapshot = copy.deepcopy(cached["data"])
    else:
        data = loading_matchup_payload(team_id, cache_version=CACHE_VERSION)
        data.pop("loading", None)
        data.pop("refreshing", None)
        prev_snapshot = None

    old_away = int((data.get("away") or {}).get("teamId") or 0)
    old_home = int((data.get("home") or {}).get("teamId") or 0)
    new_away = int(matchup["away"]["teamId"])
    new_home = int(matchup["home"]["teamId"])
    old_matchup = data.get("matchup") or {}
    old_game_sno = old_matchup.get("gameSno")
    old_game_date = old_matchup.get("date")
    new_game_sno = matchup.get("gameSno")
    new_game_date = matchup.get("date")
    # Same clubs often play consecutive days — must not keep tomorrow's starters.
    game_changed = (
        old_game_sno is not None
        and new_game_sno is not None
        and (old_game_sno != new_game_sno or old_game_date != new_game_date)
    ) or (
        bool(old_game_date)
        and bool(new_game_date)
        and old_game_date != new_game_date
    )

    data["cacheVersion"] = CACHE_VERSION
    data["focusTeamId"] = team_id
    data["matchup"] = {
        "date": matchup.get("date"),
        "gameDate": matchup.get("gameDate"),
        "gameSno": matchup.get("gameSno"),
        "status": matchup.get("status"),
        "stadium": matchup.get("stadium"),
    }

    from app.pitcher_peer_sync import (
        merge_probable_pitchers_from_cache,
        patch_probable_pitcher_header,
        pitcher_name,
    )

    def _name(panel: dict[str, Any] | None) -> str:
        return pitcher_name(panel)

    if old_away and old_home and {old_away, old_home} == {new_away, new_home}:
        if old_away == new_home and old_home == new_away:
            data["away"], data["home"] = data["home"], data["away"]
        for side in ("away", "home"):
            panel = data.get(side) or {}
            src = matchup[side]
            panel["teamId"] = src["teamId"]
            panel["teamName"] = src.get("teamName") or panel.get("teamName")
            new_pitcher = src.get("probablePitcher")
            patch_probable_pitcher_header(panel, new_pitcher, game_changed=game_changed)
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

    from app.cpbl_cache import (
        _count_final_starts_on_schedule,
        _load_schedule_games_for_upgrade,
    )
    from app.pitcher_rows import pitcher_analysis_needs_rebuild

    schedule = _load_schedule_games_for_upgrade()
    expected_by_side: dict[str, int] = {}
    for side in ("away", "home"):
        panel = data.get(side) or {}
        starter = _name(panel)
        team_id = int(panel.get("teamId") or 0)
        if starter and team_id and schedule:
            expected_by_side[side] = _count_final_starts_on_schedule(
                schedule, team_id, starter
            )

    has_starter = any(_name(data.get(side)) for side in ("away", "home"))
    needs_analysis = pitcher_analysis_needs_rebuild(
        data, game_count=games, expected_starts_by_side=expected_by_side
    )

    # Always refresh lineups on header update (game-day confirmed card).
    # Full pitcher rebuild only when analysis is missing / starter changed —
    # avoids wiping good pitcher blocks when live schedule briefly lacks names.
    try:
        from app.cpbl_service import (
            fetch_matchup_starting_lineups,
            rebuild_pitcher_dependent_fields,
        )

        lineup_matchup = {
            "date": matchup.get("date"),
            "gameDate": matchup.get("gameDate"),
            "gameSno": matchup.get("gameSno"),
            "year": int(str(matchup.get("date") or "")[:4] or 2026),
            "status": matchup.get("status"),
            "stadium": matchup.get("stadium"),
            "away": {
                "teamId": data["away"]["teamId"],
                "teamName": data["away"].get("teamName") or "",
                "probablePitcher": data["away"].get("probablePitcher"),
            },
            "home": {
                "teamId": data["home"]["teamId"],
                "teamName": data["home"].get("teamName") or "",
                "probablePitcher": data["home"].get("probablePitcher"),
            },
        }
        if needs_analysis and has_starter:
            data = await rebuild_pitcher_dependent_fields(data, game_count=games)
        else:
            lu_client = CpblClient()
            try:
                data["startingLineups"] = await fetch_matchup_starting_lineups(
                    lu_client, lineup_matchup
                )
            finally:
                await lu_client.close()
    except Exception:
        logger.exception(
            "CPBL lineup/pitcher refresh failed for team %s", team_id
        )

    if prev_snapshot:
        merge_probable_pitchers_from_cache(
            data, prev_snapshot, league="cpbl", fill_only=True
        )

    from app.inning_comparison import refresh_situational_from_panels

    data = refresh_situational_from_panels(data)
    await store_matchup(team_id, games, data)
    from app.pitcher_peer_sync import mirror_pitchers_to_peer

    await mirror_pitchers_to_peer(
        data, games, team_id, get_matchup=get_matchup, store_matchup=store_matchup
    )
    logger.info(
        "Refreshed CPBL matchup header for team %s → %s (cloudLite=%s)",
        team_id,
        matchup.get("date"),
        is_cloud_lite(),
    )


async def refresh_matchup(
    team_id: int, games: int = DEFAULT_GAMES, *, expected: ExpectedMatchup | None = None
) -> None:
    key = f"cpbl:matchup:v{CACHE_VERSION}:{team_id}:{games}"
    if key in _refreshing_keys:
        return

    _refreshing_keys.add(key)
    try:
        from app.cloud_lite import is_cloud_lite

        if is_cloud_lite():
            await refresh_matchup_header(team_id, games, expected=expected)
            return

        from app.cpbl_service import (
            invalidate_shared_schedule_cache,
            rebuild_pitcher_dependent_fields,
        )
        from app.cpbl_cache import (
            _count_final_starts_on_schedule,
            _load_schedule_games_for_upgrade,
        )
        from app.pitcher_rows import (
            cached_pitcher_analysis_complete,
            pitcher_analysis_needs_rebuild,
        )

        # Clear in-memory schedule only so status repair / pitchers re-apply.
        # Do not wipe the disk schedule file on every team refresh.
        invalidate_shared_schedule_cache(wipe_disk=False)
        previous = get_matchup(team_id, games)
        data = await analyze_matchup(team_id, games, expected=expected)
        from app.pitcher_peer_sync import merge_probable_pitchers_from_cache

        prev = (previous.get("data") or {}) if previous else {}
        pitcher_merged = (
            merge_probable_pitchers_from_cache(
                data, prev, league="cpbl", fill_only=False
            )
            if prev
            else False
        )
        # Official schedule sometimes returns blank starters briefly — keep known names
        # only when this is the same gameSno (consecutive days share opponent clubs).
        if previous:
            prev = previous.get("data") or {}
            prev_sno = (prev.get("matchup") or {}).get("gameSno")
            new_sno = (data.get("matchup") or {}).get("gameSno")
            same_game = prev_sno is not None and new_sno is not None and prev_sno == new_sno
            if same_game:
                for side in ("away", "home"):
                    new_panel = data.get(side) or {}
                    old_panel = prev.get(side) or {}
                    if new_panel.get("teamId") != old_panel.get("teamId"):
                        continue
                    new_name = ((new_panel.get("probablePitcher") or {}).get("fullName") or "").strip()
                    old_name = ((old_panel.get("probablePitcher") or {}).get("fullName") or "").strip()
                    if not new_name and old_name:
                        new_panel["probablePitcher"] = old_panel.get("probablePitcher")
                        old_analysis = old_panel.get("pitcherAnalysis")
                        if old_analysis:
                            old_games = (old_analysis or {}).get("games") or []
                            side_team_id = int(new_panel.get("teamId") or 0)
                            schedule = _load_schedule_games_for_upgrade()
                            expected = (
                                _count_final_starts_on_schedule(schedule, side_team_id, old_name)
                                if schedule and side_team_id
                                else 0
                            )
                            if cached_pitcher_analysis_complete(
                                old_games, game_count=games, expected_starts=expected
                            ):
                                new_panel["pitcherAnalysis"] = old_analysis
                        data[side] = new_panel
                    elif (
                        new_name
                        and old_name
                        and new_name == old_name
                        and not (new_panel.get("pitcherAnalysis") or {}).get("games")
                        and (old_panel.get("pitcherAnalysis") or {}).get("games")
                    ):
                        old_games = (old_panel.get("pitcherAnalysis") or {}).get("games") or []
                        side_team_id = int(new_panel.get("teamId") or 0)
                        schedule = _load_schedule_games_for_upgrade()
                        expected = (
                            _count_final_starts_on_schedule(schedule, side_team_id, new_name)
                            if schedule and side_team_id
                            else 0
                        )
                        if cached_pitcher_analysis_complete(
                            old_games, game_count=games, expected_starts=expected
                        ):
                            new_panel["pitcherAnalysis"] = old_panel.get("pitcherAnalysis")
                            data[side] = new_panel

        schedule = _load_schedule_games_for_upgrade()
        expected_by_side: dict[str, int] = {}
        for side in ("away", "home"):
            panel = data.get(side) or {}
            starter = ((panel.get("probablePitcher") or {}).get("fullName") or "").strip()
            side_team_id = int(panel.get("teamId") or 0)
            if starter and side_team_id and schedule:
                expected_by_side[side] = _count_final_starts_on_schedule(
                    schedule, side_team_id, starter
                )
        if pitcher_merged or pitcher_analysis_needs_rebuild(
            data, game_count=games, expected_starts_by_side=expected_by_side
        ):
            data = await rebuild_pitcher_dependent_fields(data, game_count=games)
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
        from app.pitcher_peer_sync import mirror_pitchers_to_peer

        await mirror_pitchers_to_peer(
            data, games, team_id, get_matchup=get_matchup, store_matchup=store_matchup
        )
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

        if cache_needs_upgrade(entry, games=games):
            stale.append(team)
    return stale


async def _teams_needing_refresh_async(teams: list[dict], games: int) -> list[dict]:
    from app.panel_freshness import cpbl_team_panels_stale

    stale = _teams_needing_refresh(teams, games)
    stale_ids = {int(t["id"]) for t in stale}
    for team in teams:
        tid = int(team["id"])
        if tid in stale_ids:
            continue
        entry = get_matchup(tid, games)
        if not entry:
            continue
        try:
            if await cpbl_team_panels_stale(tid, entry.get("data") or {}):
                stale.append(team)
        except Exception:
            logger.exception("CPBL panel stale check failed for team %s", tid)
    return stale


async def refresh_all_matchups(games: int = DEFAULT_GAMES) -> None:
    global _warming_all
    async with _refresh_lock:
        _warming_all = True
        try:
            teams = await fetch_cpbl_teams()
            targets = await _teams_needing_refresh_async(teams, games)
            if not targets:
                logger.info("CPBL cache already warm for all %s teams", len(teams))
            else:
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
            # Validate even when "warm" — stale TTL can still hold the wrong game.
            try:
                from app.data_validate import validate_cpbl_cache

                report = await validate_cpbl_cache(games=games, repair=True)
                if not report.get("ok"):
                    logger.error(
                        "CPBL validation still failing (%s issues)",
                        len(report.get("issues") or []),
                    )
            except Exception:
                logger.exception("CPBL post-refresh validation failed")
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
