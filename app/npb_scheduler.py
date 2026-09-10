"""Background refresh of cached NPB analysis."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.npb_cache import (
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
from app.npb_service import analyze_matchup, analyze_matchup_a_table, fetch_npb_teams
from app.npb_service import NpbClient, fetch_matchup_starting_lineups, fetch_next_matchup
from app.matchup_pick import ExpectedMatchup
from app.pitcher_peer_sync import (
    mirror_pitchers_from_peer_cache,
    mirror_pitchers_to_peer,
    pitcher_name as _pitcher_name,
    probable_pitchers_missing,
)

logger = logging.getLogger(__name__)


def mirror_pitchers_from_peer_cache_npb(data: dict, games: int) -> bool:
    return mirror_pitchers_from_peer_cache(data, games, get_matchup=get_matchup)


async def _mirror_pitchers_to_peer(data: dict, games: int, focus_team_id: int) -> None:
    await mirror_pitchers_to_peer(
        data,
        games,
        focus_team_id,
        get_matchup=get_matchup,
        store_matchup=store_matchup,
    )


def _apply_probable_pitchers_from_matchup(data: dict, matchup: dict[str, Any]) -> bool:
    changed = False
    for side in ("away", "home"):
        panel = data.get(side) or {}
        src = matchup.get(side) or {}
        new_pitcher = src.get("probablePitcher")
        new_name = _pitcher_name(src)
        if new_pitcher and new_name and not _pitcher_name(panel):
            panel["probablePitcher"] = new_pitcher
            data[side] = panel
            changed = True
    return changed


def _npb_jst_today() -> str:
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).date().isoformat()


def npb_header_stale(data: dict[str, Any], expected: ExpectedMatchup | None = None) -> bool:
    """True when cached header is from a past game or does not match the picked slate row."""
    from app.matchup_pick import ExpectedMatchup as Pick

    matchup = data.get("matchup") or {}
    md = str(matchup.get("date") or "")[:10]
    today = _npb_jst_today()
    if md and md < today:
        return True
    if expected and not Pick(
        date=expected.date,
        away_id=expected.away_id,
        home_id=expected.home_id,
        game_pk=expected.game_pk,
    ).matches_payload(data):
        return True
    return False


def _npb_should_live_patch(
    data: dict[str, Any], expected: ExpectedMatchup | None = None
) -> bool:
    """Today's NPB games need live schedule checks (afternoon pitcher announcements)."""
    if npb_header_stale(data, expected):
        return True
    md = str((data.get("matchup") or {}).get("date") or "")[:10]
    return md == _npb_jst_today()


def _apply_live_npb_header(
    data: dict[str, Any],
    matchup: dict[str, Any],
    *,
    team_id: int,
    games: int,
    prev_snapshot: dict[str, Any] | None,
) -> bool:
    """Patch matchup date/teams/probable pitchers from a live schedule row."""
    from app.pitcher_peer_sync import (
        merge_probable_pitchers_from_cache,
        patch_probable_pitcher_header,
    )

    old_away = int((data.get("away") or {}).get("teamId") or 0)
    old_home = int((data.get("home") or {}).get("teamId") or 0)
    new_away = int(matchup["away"]["teamId"])
    new_home = int(matchup["home"]["teamId"])
    old_matchup = data.get("matchup") or {}
    old_game_date = str(old_matchup.get("date") or "")[:10]
    new_game_date = str(matchup.get("date") or "")[:10]
    game_changed = bool(old_game_date and new_game_date and old_game_date != new_game_date)

    data["cacheVersion"] = CACHE_VERSION
    data["focusTeamId"] = team_id
    data["matchup"] = {
        "date": matchup.get("date"),
        "gameDate": matchup.get("gameDate"),
        "gameSno": matchup.get("gameSno"),
        "status": matchup.get("status"),
        "stadium": matchup.get("stadium"),
    }

    changed = game_changed
    if old_away and old_home and {old_away, old_home} == {new_away, new_home}:
        if old_away == new_home and old_home == new_away:
            data["away"], data["home"] = data["home"], data["away"]
        for side in ("away", "home"):
            panel = data.get(side) or {}
            src = matchup[side]
            panel["teamId"] = src["teamId"]
            panel["teamName"] = src.get("teamName") or panel.get("teamName")
            if patch_probable_pitcher_header(
                panel,
                src.get("probablePitcher"),
                game_changed=game_changed,
                force_refresh=True,
            ):
                changed = True
            data[side] = panel
    else:
        for side in ("away", "home"):
            src = matchup[side]
            data[side] = {
                "teamId": src["teamId"],
                "teamName": src.get("teamName") or "",
                "probablePitcher": src.get("probablePitcher"),
                "games": (data.get(side) or {}).get("games") or [],
                "summary": (data.get(side) or {}).get("summary") or {},
            }
        changed = True

    if prev_snapshot:
        merge_probable_pitchers_from_cache(
            data, prev_snapshot, league="npb", fill_only=True
        )
    return changed


async def patch_npb_header_from_live(
    team_id: int,
    games: int,
    cached: dict,
    *,
    expected: ExpectedMatchup | None = None,
) -> dict:
    """Lightweight NPB.jp schedule scrape — safe on Render cloud-lite."""
    from app.npb_service import (
        NpbClient,
        apply_playsport_probable_pitchers,
        fetch_next_matchup,
    )

    import copy

    client = NpbClient()
    try:
        matchup = await fetch_next_matchup(client, team_id, expected=expected)
        if matchup:
            await apply_playsport_probable_pitchers(matchup)
    finally:
        await client.close()

    if not matchup:
        return cached

    data = copy.deepcopy(cached.get("data") or {})
    prev_snapshot = copy.deepcopy(data)
    changed = _apply_live_npb_header(
        data,
        matchup,
        team_id=team_id,
        games=games,
        prev_snapshot=prev_snapshot,
    )
    if not changed:
        return cached

    await store_matchup(team_id, games, data)
    await _mirror_pitchers_to_peer(data, games, team_id)
    return get_matchup(team_id, games) or cached


async def ensure_npb_pitchers_fresh(
    team_id: int,
    games: int,
    cached: dict,
    *,
    expected: ExpectedMatchup | None = None,
) -> dict:
    """Patch missing probable pitchers before returning API payload."""
    from app.cloud_lite import is_cloud_lite
    from app.npb_service import (
        NpbClient,
        apply_playsport_probable_pitchers,
        fetch_next_matchup,
        rebuild_pitcher_dependent_fields,
    )

    data = (cached.get("data") or {}) if cached else {}
    if is_cloud_lite():
        if cached and _npb_should_live_patch(data, expected):
            return await patch_npb_header_from_live(
                team_id, games, cached, expected=expected
            )
        return cached

    import copy

    data = copy.deepcopy(data)
    if _npb_should_live_patch(data, expected):
        return await patch_npb_header_from_live(
            team_id, games, cached, expected=expected
        )
    if not probable_pitchers_missing(data):
        return cached

    changed = mirror_pitchers_from_peer_cache_npb(data, games)
    if changed:
        await store_matchup(team_id, games, data)
        await _mirror_pitchers_to_peer(data, games, team_id)
        cached = get_matchup(team_id, games) or cached
        data = copy.deepcopy(cached.get("data") or {})
        if not probable_pitchers_missing(data):
            return cached

    client = NpbClient()
    try:
        matchup = await fetch_next_matchup(client, team_id, expected=expected)
        if matchup:
            await apply_playsport_probable_pitchers(matchup)
            changed = _apply_probable_pitchers_from_matchup(data, matchup)
    finally:
        await client.close()

    if not changed:
        return cached

    from app.pitcher_rows import pitcher_analysis_missing_pitch_counts

    has_starter = any(_pitcher_name(data.get(side)) for side in ("away", "home"))
    needs_analysis = pitcher_analysis_missing_pitch_counts(data) or any(
        _pitcher_name(data.get(side))
        and not ((data.get(side) or {}).get("pitcherAnalysis") or {}).get("games")
        for side in ("away", "home")
    )
    if has_starter and needs_analysis:
        try:
            data = await rebuild_pitcher_dependent_fields(data, game_count=games)
        except Exception:
            logger.exception("NPB pitcher rebuild failed during ensure for team %s", team_id)

    await store_matchup(team_id, games, data)
    await _mirror_pitchers_to_peer(data, games, team_id)
    return get_matchup(team_id, games) or cached

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3600"))
WARMUP_CONCURRENCY = int(os.environ.get("NPB_WARMUP_CONCURRENCY", "2"))

_refresh_lock = asyncio.Lock()
_refreshing_keys: set[str] = set()
_warming_all = False

def is_refreshing(team_id: int, games: int = DEFAULT_GAMES) -> bool:
    key = f"npb:matchup:v{CACHE_VERSION}:{team_id}:{games}"
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
        logger.info("Refreshed NPB a-table cache for team %s", team_id)
    except Exception:
        logger.exception("Failed to refresh NPB a-table for team %s", team_id)
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
        raise ValueError(f"無法產生 NPB a 表格（team {team_id}）")
    return cached


async def refresh_matchup_header(
    team_id: int, games: int = DEFAULT_GAMES, *, expected: ExpectedMatchup | None = None
) -> None:
    """Cloud-safe: mirror GitHub Pages snapshot (no live NPB crawl on free tier)."""
    from app.cloud_lite import is_cloud_lite
    from app.loading_response import loading_matchup_payload
    from app.pages_mirror import seed_matchup_from_pages

    # Free tier: never call NPB network for full next-game rebuild here.
    if is_cloud_lite():
        await seed_matchup_from_pages(
            "npb", team_id, games, store=store_matchup, cache_version=CACHE_VERSION
        )
        return

    cached = get_matchup(team_id, games)
    if not cached:
        await seed_matchup_from_pages(
            "npb", team_id, games, store=store_matchup, cache_version=CACHE_VERSION
        )
        cached = get_matchup(team_id, games)

    matchup = None
    client = NpbClient()
    try:
        matchup = await fetch_next_matchup(client, team_id, expected=expected)
        if matchup:
            from app.npb_service import apply_playsport_probable_pitchers

            await apply_playsport_probable_pitchers(matchup)
    except Exception:
        logger.exception("NPB header fetch failed for team %s (keeping Pages/cache)", team_id)
    finally:
        await client.close()
    if not matchup:
        return

    import copy

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

    def _name(panel: dict | None) -> str:
        return pitcher_name(panel)

    if game_changed:
        data["startingLineups"] = {"away": {"batters": []}, "home": {"batters": []}}

    if old_away and old_home and {old_away, old_home} == {new_away, new_home}:
        if old_away == new_home and old_home == new_away:
            data["away"], data["home"] = data["home"], data["away"]
        for side in ("away", "home"):
            panel = data.get(side) or {}
            src = matchup[side]
            panel["teamId"] = src["teamId"]
            panel["teamName"] = src.get("teamName") or src.get("nameZh") or panel.get("teamName")
            new_pitcher = src.get("probablePitcher")
            patch_probable_pitcher_header(
                panel,
                new_pitcher,
                game_changed=game_changed,
                force_refresh=True,
            )
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
                "teamName": src.get("teamName") or src.get("nameZh") or "",
                "probablePitcher": src.get("probablePitcher"),
                "games": [],
                "summary": dict(empty_summary),
            }
        data["startingLineups"] = {"away": {"batters": []}, "home": {"batters": []}}
        data.pop("aTable", None)
        data.pop("situational", None)

    from app.pitcher_rows import pitcher_analysis_missing_pitch_counts

    if prev_snapshot:
        merge_probable_pitchers_from_cache(
            data, prev_snapshot, league="npb", fill_only=True
        )
    has_starter = any(_name(data.get(side)) for side in ("away", "home"))
    needs_analysis = pitcher_analysis_missing_pitch_counts(data) or any(
        _name(data.get(side))
        and not ((data.get(side) or {}).get("pitcherAnalysis") or {}).get("games")
        for side in ("away", "home")
    )
    if needs_analysis and has_starter:
        try:
            from app.npb_service import rebuild_pitcher_dependent_fields

            data = await rebuild_pitcher_dependent_fields(data, game_count=games)
        except Exception:
            logger.exception(
                "NPB pitcher rebuild failed for team %s (header kept)", team_id
            )

    try:
        lineup_matchup = {
            "date": matchup.get("date"),
            "gameDate": matchup.get("gameDate"),
            "gameSno": matchup.get("gameSno"),
            "status": matchup.get("status"),
            "href": matchup.get("href"),
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
        client = NpbClient()
        try:
            data["startingLineups"] = await fetch_matchup_starting_lineups(
                client, lineup_matchup
            )
        finally:
            await client.close()
    except Exception:
        logger.exception("NPB lineup header refresh failed for team %s", team_id)

    from app.inning_comparison import refresh_situational_from_panels

    data = refresh_situational_from_panels(data)
    await store_matchup(team_id, games, data)
    await _mirror_pitchers_to_peer(data, games, team_id)
    logger.info("Refreshed NPB matchup header for team %s", team_id)


async def refresh_matchup(
    team_id: int, games: int = DEFAULT_GAMES, *, expected: ExpectedMatchup | None = None
) -> None:
    key = f"npb:matchup:v{CACHE_VERSION}:{team_id}:{games}"
    if key in _refreshing_keys:
        return

    _refreshing_keys.add(key)
    try:
        from app.cloud_lite import is_cloud_lite

        if is_cloud_lite():
            await refresh_matchup_header(team_id, games, expected=expected)
            return

        previous = get_matchup(team_id, games)
        data = await analyze_matchup(team_id, games, expected=expected)
        from app.pitcher_peer_sync import restore_probable_pitchers_if_same_game

        if previous:
            prev_data = previous.get("data") or {}
            if restore_probable_pitchers_if_same_game(data, prev_data, league="npb"):
                try:
                    from app.npb_service import rebuild_pitcher_dependent_fields

                    data = await rebuild_pitcher_dependent_fields(data, game_count=games)
                except Exception:
                    logger.exception(
                        "NPB pitcher restore rebuild failed for team %s", team_id
                    )
        data["cacheVersion"] = CACHE_VERSION
        client = NpbClient()
        try:
            matchup = await fetch_next_matchup(client, team_id, expected=expected)
            if matchup:
                data["startingLineups"] = await fetch_matchup_starting_lineups(client, matchup)
        except Exception:
            logger.exception("Failed to fetch NPB lineups during refresh for team %s", team_id)
        finally:
            await client.close()
        await store_matchup(team_id, games, data)
        await _mirror_pitchers_to_peer(data, games, team_id)
        if a_table := data.get("aTable"):
            await store_a_table(team_id, a_table)
        logger.info("Refreshed NPB matchup cache for team %s (%s games)", team_id, games)
    except Exception:
        logger.exception("Failed to refresh NPB matchup for team %s", team_id)
    finally:
        _refreshing_keys.discard(key)


def _teams_needing_refresh(teams: list[dict], games: int) -> list[dict]:
    stale: list[dict] = []
    for team in teams:
        entry = get_matchup(team["id"], games)
        if entry is None or is_stale(entry["updatedAt"]):
            stale.append(team)
            continue
        from app.npb_cache import cache_needs_upgrade

        if cache_needs_upgrade(entry):
            stale.append(team)
            continue
        if probable_pitchers_missing(entry.get("data") or {}):
            stale.append(team)
    return stale


async def _teams_needing_refresh_async(teams: list[dict], games: int) -> list[dict]:
    from app.panel_freshness import npb_team_panels_stale

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
            if await npb_team_panels_stale(tid, entry.get("data") or {}):
                stale.append(team)
        except Exception:
            logger.exception("NPB panel stale check failed for team %s", tid)
    return stale


async def _pitcher_header_refresh_loop() -> None:
    """Keep today's probable pitchers in sync across per-team caches."""
    await asyncio.sleep(120)
    while True:
        try:
            teams = await fetch_npb_teams()
            for team in teams:
                entry = get_matchup(team["id"], DEFAULT_GAMES)
                if entry and probable_pitchers_missing(entry.get("data") or {}):
                    await refresh_matchup_header(team["id"], DEFAULT_GAMES)
        except Exception:
            logger.exception("NPB pitcher header loop failed")
        await asyncio.sleep(600)


async def refresh_all_matchups(games: int = DEFAULT_GAMES) -> None:
    global _warming_all
    async with _refresh_lock:
        _warming_all = True
        try:
            teams = await fetch_npb_teams()
            targets = await _teams_needing_refresh_async(teams, games)
            if not targets:
                logger.info("NPB cache already warm for all %s teams", len(teams))
            else:
                semaphore = asyncio.Semaphore(WARMUP_CONCURRENCY)

                async def refresh_one(team: dict) -> None:
                    async with semaphore:
                        await refresh_matchup(team["id"], games)

                await asyncio.gather(*[refresh_one(team) for team in targets])
                logger.info(
                    "Finished warming NPB cache (%s/%s teams refreshed)",
                    len(targets),
                    len(teams),
                )
            try:
                from app.data_validate import validate_npb_cache

                report = await validate_npb_cache(games=games)
                if not report.get("ok"):
                    logger.error(
                        "NPB validation issues (%s): %s",
                        len(report.get("issues") or []),
                        (report.get("issues") or [])[:8],
                    )
            except Exception:
                logger.exception("NPB post-refresh validation failed")
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


async def start_npb_cache_services(*, skip_load: bool = False) -> None:
    if not skip_load:
        load_from_disk()
    logger.info(
        "NPB cache loaded (%s teams on disk).",
        cached_team_count(DEFAULT_GAMES),
    )
    from app.cloud_lite import is_cloud_lite

    if not is_cloud_lite():
        asyncio.create_task(hourly_refresh_loop())
        asyncio.create_task(_pitcher_header_refresh_loop())