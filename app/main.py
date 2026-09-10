from contextlib import asynccontextmanager
import contextlib
import copy
from pathlib import Path
from typing import Any

import asyncio

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.cache import (
    CACHE_VERSION as MLB_CACHE_VERSION,
    DEFAULT_GAMES,
    cache_needs_upgrade as mlb_cache_needs_upgrade,
    cached_team_count as mlb_cached_team_count,
    get_a_table as get_mlb_a_table,
    get_matchup,
    is_stale,
    store_matchup as store_mlb_matchup,
    wrap_a_table_response as wrap_mlb_a_table_response,
    wrap_matchup_response,
)
import httpx

from app.mlb_service import (
    fetch_matchup_starting_lineups as fetch_mlb_starting_lineups,
    fetch_next_matchup as fetch_mlb_next_matchup,
    fetch_teams,
    lineups_need_rebuild as mlb_lineups_need_rebuild,
    lineups_trusted_for_matchup as mlb_lineups_trusted,
    matchup_dict_from_cached_data,
    matchup_header_needs_immediate_refresh,
    mlb_cache_game_pk_mismatch,
)
from app.npb_cache import (
    CACHE_VERSION as NPB_CACHE_VERSION,
    cache_needs_upgrade as npb_cache_needs_upgrade,
    cached_team_count as npb_cached_team_count,
    get_a_table as get_npb_a_table,
    get_matchup as get_npb_matchup,
    is_stale as npb_is_stale,
    store_matchup as store_npb_matchup,
    wrap_a_table_response as wrap_npb_a_table_response,
    wrap_matchup_response as wrap_npb_matchup_response,
)
from app.npb_display import localize_starting_lineups
from app.npb_service import (
    NpbClient,
    fetch_matchup_starting_lineups as fetch_npb_starting_lineups,
    fetch_next_matchup as fetch_npb_next_matchup,
    fetch_npb_teams,
    npb_lineups_need_rebuild,
)
from app.cpbl_cache import (
    CACHE_VERSION as CPBL_CACHE_VERSION,
    cache_needs_upgrade as cpbl_cache_needs_upgrade,
    cached_team_count as cpbl_cached_team_count,
    get_a_table as get_cpbl_a_table,
    get_matchup as get_cpbl_matchup,
    is_stale as cpbl_is_stale,
    store_matchup as store_cpbl_matchup,
    wrap_a_table_response as wrap_cpbl_a_table_response,
    wrap_matchup_response as wrap_cpbl_matchup_response,
)
from app.cpbl_service import (
    CpblClient,
    cpbl_lineups_need_rebuild,
    fetch_cpbl_teams,
    fetch_matchup_starting_lineups,
    fetch_next_matchup,
)
from app.cpbl_verify import verify_cpbl
from app.cpbl_scheduler import refresh_matchup as refresh_cpbl_matchup
from app.cpbl_scheduler import is_refreshing as cpbl_is_refreshing
from app.cpbl_scheduler import is_refreshing_a_table as cpbl_is_refreshing_a_table
from app.cpbl_scheduler import is_warming_all as cpbl_is_warming_all
from app.cpbl_scheduler import ensure_a_table as ensure_cpbl_a_table
from app.cpbl_scheduler import refresh_a_table as refresh_cpbl_a_table
from app.cpbl_scheduler import refresh_all_matchups as refresh_all_cpbl_matchups
from app.cpbl_scheduler import start_cpbl_cache_services
from app.loading_response import loading_matchup_payload
from app.npb_scheduler import refresh_matchup as refresh_npb_matchup
from app.npb_scheduler import is_refreshing as npb_is_refreshing
from app.npb_scheduler import is_refreshing_a_table as npb_is_refreshing_a_table
from app.npb_scheduler import is_warming_all as npb_is_warming_all
from app.npb_scheduler import ensure_a_table as ensure_npb_a_table
from app.npb_scheduler import refresh_a_table as refresh_npb_a_table
from app.npb_scheduler import refresh_all_matchups as refresh_all_npb_matchups
from app.npb_scheduler import start_npb_cache_services
from app.cloud_keepalive import cloud_keepalive_loop
from app.cloud_lite import is_cloud_lite
from app.scheduler import refresh_matchup, is_refreshing as mlb_is_refreshing
from app.scheduler import is_refreshing_a_table as mlb_is_refreshing_a_table
from app.scheduler import is_warming_all as mlb_is_warming_all
from app.scheduler import ensure_a_table as ensure_mlb_a_table
from app.scheduler import refresh_a_table as refresh_mlb_a_table
from app.scheduler import refresh_all_matchups as refresh_all_mlb_matchups
from app.scheduler import start_cache_services

from app.inning_comparison import a_table_payload_complete
from app.matchup_integrity import blank_lineups_for_matchup
from app.matchup_pick import ExpectedMatchup

BASE_DIR = Path(__file__).resolve().parent.parent


def _schedule(coro) -> None:
    # Always schedule on-demand refresh (force / stale / cache miss).
    # CLOUD_LITE only disables keepalive + full warm-all loops, not user refresh.
    asyncio.create_task(coro)


_cloud_refresh_slots = 0
_CLOUD_REFRESH_MAX = 1


def _can_start_cloud_refresh() -> bool:
    """Render free tier OOMs if multiple matchup rebuilds overlap."""
    global _cloud_refresh_slots
    if not is_cloud_lite():
        return True
    if _cloud_refresh_slots >= _CLOUD_REFRESH_MAX:
        return False
    _cloud_refresh_slots += 1
    return True


def _finish_cloud_refresh() -> None:
    global _cloud_refresh_slots
    if _cloud_refresh_slots > 0:
        _cloud_refresh_slots -= 1


async def _schedule_matchup_refresh(factory, *, force: bool) -> bool:
    """Schedule at most one cloud rebuild. factory() must return an awaitable."""
    if not is_cloud_lite():
        _schedule(factory())
        return True

    if _can_start_cloud_refresh():
        async def _runner():
            try:
                await factory()
            finally:
                _finish_cloud_refresh()

        _schedule(_runner())
        return True

    # Another refresh is running — caller should keep polling on force.
    return False


def _needs_full_matchup_rebuild(
    cached: dict | None, *, league: str, games: int = DEFAULT_GAMES
) -> bool:
    """Full analyze_matchup rebuild — only when panels are missing or structurally stale."""
    if not cached:
        return True
    data = cached.get("data") or {}
    if league == "mlb":
        if mlb_cache_needs_upgrade(cached):
            return True
    elif league == "cpbl":
        if cpbl_cache_needs_upgrade(cached, games=games):
            return True
    elif league == "npb":
        if npb_cache_needs_upgrade(cached):
            return True

    from app.pitcher_peer_sync import probable_pitchers_missing
    from app.pitcher_rows import pitcher_analysis_missing_pitch_counts

    if probable_pitchers_missing(data):
        return True
    if pitcher_analysis_missing_pitch_counts(data):
        return True
    if not ((data.get("away") or {}).get("games") or []):
        return True
    return False


async def _background_matchup_refresh(
    *,
    refresh_header,
    full_refresh_factory,
    team_id: int,
    games: int,
    force: bool,
    expected=None,
    needs_full_rebuild: bool = True,
) -> None:
    try:
        await refresh_header(team_id, games, expected=expected)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Matchup header refresh failed for team %s", team_id
        )
    if needs_full_rebuild or force:
        await _schedule_matchup_refresh(full_refresh_factory, force=force)


def _mlb_needs_timing_patch(cached: dict | None) -> bool:
    if not cached:
        return False
    matchup_meta = (cached.get("data") or {}).get("matchup") or {}
    return bool(
        matchup_meta.get("gameDate")
        and (not matchup_meta.get("stadium") or not matchup_meta.get("timeTaiwan"))
    )


def _kick_matchup_refresh_if_needed(
    *,
    team_id: int,
    games: int,
    force: bool,
    cached: dict | None,
    needs_refresh: bool,
    needs_timing_patch: bool,
    is_refreshing_fn,
    refresh_header,
    full_refresh_factory,
    expected=None,
    needs_full_rebuild: bool = True,
) -> bool:
    """Schedule header + panel refresh in background; return refreshing flag."""
    if not (needs_refresh or needs_timing_patch):
        return (not is_cloud_lite()) and is_refreshing_fn(team_id, games)

    if is_refreshing_fn(team_id, games):
        return True

    _schedule(
        _background_matchup_refresh(
            refresh_header=refresh_header,
            full_refresh_factory=full_refresh_factory,
            team_id=team_id,
            games=games,
            force=force,
            expected=expected,
            needs_full_rebuild=needs_full_rebuild,
        )
    )
    return True


async def _ensure_mlb_game_current(
    team_id: int,
    games: int,
    cached: dict | None,
    *,
    expected: ExpectedMatchup | None,
    refresh_header,
) -> dict | None:
    if not cached or expected is not None or is_cloud_lite():
        return cached
    if mlb_is_refreshing(team_id, games):
        return cached
    needs = matchup_header_needs_immediate_refresh(cached)
    if not needs:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                needs = await mlb_cache_game_pk_mismatch(
                    client, team_id, cached, expected=expected
                )
        except Exception:
            return cached
    if not needs:
        return cached
    await refresh_header(team_id, games, expected=expected)
    return get_matchup(team_id, games)


async def _ensure_cached_matchup_for_expected(
    *,
    team_id: int,
    games: int,
    expected: ExpectedMatchup | None,
    get_matchup_fn,
    refresh_header_fn,
    refresh_full_fn,
    is_refreshing_fn,
) -> dict | None:
    """When the user picked a slate row, sync cache to that game before serving data."""
    cached = get_matchup_fn(team_id, games)
    if expected is None:
        return cached
    if cached and expected.matches_cache_entry(cached):
        return cached
    if is_refreshing_fn(team_id, games):
        return cached
    try:
        await refresh_header_fn(team_id, games, expected=expected)
    except Exception:
        logging.getLogger(__name__).exception(
            "Matchup header refresh failed for team %s (expected pick)", team_id
        )
    cached = get_matchup_fn(team_id, games)
    if cached and expected.matches_cache_entry(cached):
        data = (cached.get("data") or {})
        away_id = int((data.get("away") or {}).get("teamId") or 0)
        home_id = int((data.get("home") or {}).get("teamId") or 0)
        pick_ids = {expected.away_id, expected.home_id} - {None}
        if pick_ids and {away_id, home_id} == pick_ids and _panels_usable(data, games):
            return cached
    if is_refreshing_fn(team_id, games):
        return cached
    try:
        await refresh_full_fn(team_id, games, expected=expected)
    except Exception:
        logging.getLogger(__name__).exception(
            "Matchup full refresh failed for team %s (expected pick)", team_id
        )
    return get_matchup_fn(team_id, games)


def _expected_cache_mismatch(
    expected: ExpectedMatchup | None, cached: dict | None
) -> bool:
    return bool(
        expected is not None
        and (cached is None or not expected.matches_cache_entry(cached))
    )


async def _await_matchup_refresh_if_forced(
    *,
    force: bool,
    team_id: int,
    games: int,
    needs_refresh: bool,
    needs_timing_patch: bool,
    is_refreshing_fn,
    refresh_header,
    full_refresh_factory,
    expected=None,
    needs_full_rebuild: bool = True,
    expected_mismatch: bool = False,
    panels_stale: bool = False,
) -> bool:
    """Run refresh synchronously when force=true or panels lag; else background."""
    if not (needs_refresh or needs_timing_patch):
        return (not is_cloud_lite()) and is_refreshing_fn(team_id, games)

    if is_refreshing_fn(team_id, games):
        return True

    coro = _background_matchup_refresh(
        refresh_header=refresh_header,
        full_refresh_factory=full_refresh_factory,
        team_id=team_id,
        games=games,
        force=force,
        expected=expected,
        needs_full_rebuild=needs_full_rebuild,
    )
    sync = (force and not is_cloud_lite()) or (
        expected_mismatch and expected is not None and not is_cloud_lite()
    )
    if sync:
        await coro
        return False
    _schedule(coro)
    return True


def _attach_a_table(
    payload: dict,
    team_id: int,
    *,
    get_table,
    is_refreshing_table,
    refresh_table,
) -> dict:
    if a_table_payload_complete(payload.get("aTable") or {}):
        return payload

    entry = get_table(team_id)
    if entry and entry.get("data") and a_table_payload_complete(entry["data"]):
        merged = copy.deepcopy(payload)
        merged["aTable"] = copy.deepcopy(entry["data"])
        return merged

    if not is_refreshing_table(team_id) and not is_cloud_lite():
        _schedule(refresh_table(team_id))
    return payload


async def _wrap_npb_matchup(
    team_id: int, entry: dict, *, refreshing: bool, games: int = DEFAULT_GAMES
) -> dict:
    from app.npb_cache import get_matchup as get_npb_matchup_entry, store_matchup as store_npb_matchup
    from app.npb_scheduler import ensure_npb_pitchers_fresh
    from app.pitcher_peer_sync import sync_pitchers_on_read

    entry = await sync_pitchers_on_read(
        team_id,
        games,
        entry,
        get_matchup=get_npb_matchup_entry,
        store_matchup=store_npb_matchup,
        ensure_fresh=ensure_npb_pitchers_fresh,
    )
    payload = await asyncio.to_thread(
        wrap_npb_matchup_response, entry, refreshing=refreshing
    )
    return _attach_a_table(
        payload,
        team_id,
        get_table=get_npb_a_table,
        is_refreshing_table=npb_is_refreshing_a_table,
        refresh_table=refresh_npb_a_table,
    )


def _lineups_have_card(lineups: dict | None) -> bool:
    if not isinstance(lineups, dict):
        return False
    away = len((lineups.get("away") or {}).get("batters") or [])
    home = len((lineups.get("home") or {}).get("batters") or [])
    return away >= 7 or home >= 7


async def _wrap_cpbl_matchup(
    team_id: int, entry: dict, *, refreshing: bool, games: int = DEFAULT_GAMES
) -> dict:
    from app.cpbl_cache import get_matchup as get_cpbl_entry, store_matchup as store_cpbl_entry
    from app.pitcher_peer_sync import sync_pitchers_on_read

    entry = await sync_pitchers_on_read(
        team_id,
        games,
        entry,
        get_matchup=get_cpbl_entry,
        store_matchup=store_cpbl_entry,
    )
    payload = wrap_cpbl_matchup_response(entry, refreshing=refreshing)
    return _attach_a_table(
        payload,
        team_id,
        get_table=get_cpbl_a_table,
        is_refreshing_table=cpbl_is_refreshing_a_table,
        refresh_table=refresh_cpbl_a_table,
    )


def _panels_usable(data: dict[str, Any] | None, games: int, *, min_games: int = 5) -> bool:
    if not data:
        return False
    need = min(min_games, games)
    away = data.get("away") or {}
    home = data.get("home") or {}
    ag = len(away.get("games") or [])
    hg = len(home.get("games") or [])
    if ag < need or hg < need:
        return False
    for side in (away, home):
        for game in side.get("games") or []:
            if game.get("teamScore") is None or game.get("opponentScore") is None:
                return False
    return True


def _mlb_refreshing_for_response(cached: dict | None, refreshing: bool, games: int) -> bool:
    if not refreshing:
        return False
    data = (cached or {}).get("data") or {}
    return not _panels_usable(data, games)


async def _wrap_mlb_matchup(
    team_id: int, entry: dict, *, refreshing: bool, games: int = DEFAULT_GAMES
) -> dict:
    from app.cache import get_matchup as get_mlb_entry, store_matchup as store_mlb_entry
    from app.mlb_display import apply_mlb_matchup_timing
    from app.pitcher_peer_sync import sync_pitchers_on_read

    entry = await sync_pitchers_on_read(
        team_id,
        games,
        entry,
        get_matchup=get_mlb_entry,
        store_matchup=store_mlb_entry,
    )
    payload = wrap_matchup_response(entry, refreshing=refreshing)
    payload = apply_mlb_matchup_timing(payload)
    return _attach_a_table(
        payload,
        team_id,
        get_table=get_mlb_a_table,
        is_refreshing_table=mlb_is_refreshing_a_table,
        refresh_table=refresh_mlb_a_table,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.team_names import localize_analysis as _check_localize

    _ = _check_localize  # fail fast at startup if cache imports break

    async def _boot_cache_services() -> None:
        import logging

        from app.cache import load_from_disk as load_mlb_disk
        from app.cpbl_cache import load_from_disk as load_cpbl_disk
        from app.npb_cache import load_from_disk as load_npb_disk

        logger = logging.getLogger(__name__)
        try:
            # Let /health succeed before heavy disk IO on free tier.
            if is_cloud_lite():
                await asyncio.sleep(2)
                await asyncio.to_thread(load_mlb_disk)
                await asyncio.sleep(0.5)
                await asyncio.to_thread(load_npb_disk)
                await asyncio.sleep(0.5)
                await asyncio.to_thread(load_cpbl_disk)
                await start_cache_services(skip_load=True)
                await start_npb_cache_services(skip_load=True)
                await start_cpbl_cache_services(skip_load=True)
                logger.info("CLOUD_LITE boot complete")
                return
            await asyncio.to_thread(load_mlb_disk)
            await asyncio.sleep(1)
            await asyncio.to_thread(load_npb_disk)
            await asyncio.sleep(1)
            await asyncio.to_thread(load_cpbl_disk)
            await start_cache_services(skip_load=True)
            await start_npb_cache_services(skip_load=True)
            await start_cpbl_cache_services(skip_load=True)
            from app.pitcher_peer_sync import sync_all_peer_pitchers_for_league
            from app.cache import get_matchup as get_mlb_matchup, store_matchup as store_mlb_matchup
            from app.cpbl_cache import get_matchup as get_cpbl_matchup, store_matchup as store_cpbl_matchup
            from app.npb_cache import get_matchup as get_npb_matchup_entry, store_matchup as store_npb_matchup
            from app.npb_teams import list_teams as npb_team_list

            mlb_fixed = await sync_all_peer_pitchers_for_league(
                list(range(1, 31)),
                DEFAULT_GAMES,
                get_matchup=get_mlb_matchup,
                store_matchup=store_mlb_matchup,
            )
            npb_fixed = await sync_all_peer_pitchers_for_league(
                [team["id"] for team in npb_team_list()],
                DEFAULT_GAMES,
                get_matchup=get_npb_matchup_entry,
                store_matchup=store_npb_matchup,
            )
            cpbl_fixed = await sync_all_peer_pitchers_for_league(
                list(range(1, 7)),
                DEFAULT_GAMES,
                get_matchup=get_cpbl_matchup,
                store_matchup=store_cpbl_matchup,
            )
            logger.info(
                "Peer pitcher sync: MLB %s, NPB %s, CPBL %s teams patched",
                mlb_fixed,
                npb_fixed,
                cpbl_fixed,
            )
            logger.info("Background cache boot complete")
        except Exception:
            logger.exception("Background cache boot failed")

    boot_task = asyncio.create_task(_boot_cache_services())
    keepalive_task = None
    if not is_cloud_lite():
        keepalive_task = asyncio.create_task(cloud_keepalive_loop())
    try:
        yield
    finally:
        boot_task.cancel()
        if keepalive_task:
            keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await boot_task
            if keepalive_task:
                await keepalive_task


app = FastAPI(title="棒球前五局分析", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://jay41004.github.io",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_origin_regex=r"https://.*\.github\.io",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_browser_cache_for_local_ui(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path in {"/", "/npb", "/cpbl", "/slate"}:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/meta")
async def api_meta():
    mlb_cached = mlb_cached_team_count(DEFAULT_GAMES)
    npb_cached = npb_cached_team_count(DEFAULT_GAMES)
    cpbl_cached = cpbl_cached_team_count(DEFAULT_GAMES)
    return {
        "deployMark": "2026-09-01-pitcher-peer-sync",
        "cloudLite": is_cloud_lite(),
        "renderEnv": bool(__import__("os").environ.get("RENDER")),
        "cloudLiteEnv": __import__("os").environ.get("CLOUD_LITE", ""),
        "mlbCacheVersion": MLB_CACHE_VERSION,
        "npbCacheVersion": NPB_CACHE_VERSION,
        "cpblCacheVersion": CPBL_CACHE_VERSION,
        "mlbTeamsCached": mlb_cached,
        "npbTeamsCached": npb_cached,
        "cpblTeamsCached": cpbl_cached,
        "mlbTeamsTotal": 30,
        "npbTeamsTotal": 12,
        "cpblTeamsTotal": 6,
        "cacheReady": mlb_cached >= 30 and npb_cached >= 12 and cpbl_cached >= 6,
        "warming": mlb_is_warming_all() or npb_is_warming_all() or cpbl_is_warming_all(),
    }


@app.get("/api/warmup")
async def api_warmup():
    """Lightweight keepalive — do not rebuild all caches on every ping."""
    mlb_cached = mlb_cached_team_count(DEFAULT_GAMES)
    npb_cached = npb_cached_team_count(DEFAULT_GAMES)
    cpbl_cached = cpbl_cached_team_count(DEFAULT_GAMES)
    ready = mlb_cached >= 30 and npb_cached >= 12 and cpbl_cached >= 6

    if not ready and not is_cloud_lite():
        if mlb_cached < 30 and not mlb_is_warming_all():
            _schedule(refresh_all_mlb_matchups(DEFAULT_GAMES))
        if npb_cached < 12 and not npb_is_warming_all():
            _schedule(refresh_all_npb_matchups(DEFAULT_GAMES))
        if cpbl_cached < 6 and not cpbl_is_warming_all():
            _schedule(refresh_all_cpbl_matchups(DEFAULT_GAMES))

    return {
        "status": "ready" if ready else "warming",
        "mlbTeamsCached": mlb_cached,
        "npbTeamsCached": npb_cached,
        "cpblTeamsCached": cpbl_cached,
        "cacheReady": ready,
        "warming": not ready,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/npb", response_class=HTMLResponse)
async def npb_index(request: Request):
    return templates.TemplateResponse("npb.html", {"request": request})


@app.get("/cpbl", response_class=HTMLResponse)
async def cpbl_index(request: Request):
    return templates.TemplateResponse("cpbl.html", {"request": request})


@app.get("/slate", response_class=HTMLResponse)
async def slate_index(request: Request):
    return templates.TemplateResponse("slate.html", {"request": request})


@app.get("/api/slate")
async def api_slate(league: str | None = None):
    from app.slate_service import fetch_all_slates, fetch_league_slate

    try:
        if league:
            return await fetch_league_slate(league)
        return await fetch_all_slates()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"賽程載入失敗: {exc}") from exc


@app.get("/api/teams")
async def api_teams():
    return await fetch_teams()


@app.get("/api/npb/teams")
async def api_npb_teams():
    return await fetch_npb_teams()


@app.get("/api/cpbl/teams")
async def api_cpbl_teams():
    return await fetch_cpbl_teams()


async def _serve_mlb_picked_matchup(
    team_id: int,
    games: int,
    expected: ExpectedMatchup,
    *,
    force: bool = False,
) -> dict:
    """User picked a slate game: live fetch that exact gamePk, skip stale team cache."""
    from app.mlb_service import analyze_matchup

    cached = get_matchup(team_id, games)
    if (
        not force
        and cached
        and expected.game_pk
        and ((cached.get("data") or {}).get("matchup") or {}).get("gamePk")
        != expected.game_pk
    ):
        cached = None
    if (
        not force
        and cached
        and expected.matches_cache_entry(cached)
        and _panels_usable((cached.get("data") or {}), games)
    ):
        return await _wrap_mlb_matchup(team_id, cached, refreshing=False, games=games)

    data = await analyze_matchup(
        team_id,
        games,
        expected=expected,
        game_pk=expected.game_pk,
    )
    await store_mlb_matchup(team_id, games, data)
    entry = get_matchup(team_id, games)
    if not entry:
        return loading_matchup_payload(team_id, cache_version=MLB_CACHE_VERSION)
    return await _wrap_mlb_matchup(team_id, entry, refreshing=False, games=games)


@app.get("/api/matchup")
async def api_matchup(
    team_id: int = Query(..., description="Selected MLB team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30, description="Number of recent games"),
    force: bool = Query(False, description="Force refresh from MLB API"),
    expected_date: str | None = Query(None, description="Picked slate game date (YYYY-MM-DD)"),
    expected_away: int | None = Query(None, description="Picked away team ID"),
    expected_home: int | None = Query(None, description="Picked home team ID"),
    expected_game_pk: int | None = Query(None, description="Picked slate gamePk"),
):
    try:
        from app.slate_service import align_expected_with_slate

        expected = ExpectedMatchup.from_query(
            expected_date=expected_date,
            expected_away=expected_away,
            expected_home=expected_home,
            expected_game_pk=expected_game_pk,
        )
        expected = await align_expected_with_slate(team_id, expected)
        if expected is None:
            raise HTTPException(
                status_code=404,
                detail="今日／明日賽程表找不到此隊比賽",
            )
        return await _serve_mlb_picked_matchup(team_id, games, expected, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MLB API error: {exc}") from exc


@app.get("/api/npb/matchup")
async def api_npb_matchup(
    team_id: int = Query(..., ge=1, le=12, description="Selected NPB team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30, description="Number of recent games"),
    force: bool = Query(False, description="Force refresh from NPB.jp"),
    expected_date: str | None = Query(None),
    expected_away: int | None = Query(None),
    expected_home: int | None = Query(None),
):
    try:
        from app.npb_scheduler import refresh_matchup_header as refresh_npb_header
        from app.npb_scheduler import ensure_npb_pitchers_fresh, probable_pitchers_missing

        expected = ExpectedMatchup.from_query(
            expected_date=expected_date,
            expected_away=expected_away,
            expected_home=expected_home,
        )
        cached = get_npb_matchup(team_id, games)
        needs_pitcher_patch = bool(
            cached and probable_pitchers_missing(cached.get("data") or {})
        )
        needs_full_rebuild = force or _needs_full_matchup_rebuild(
            cached, league="npb", games=games
        )
        needs_refresh = (
            needs_full_rebuild
            or cached is None
            or npb_is_stale(cached["updatedAt"])
            or (expected is not None and not expected.matches_cache_entry(cached))
            or needs_pitcher_patch
        )
        if cached and not force:
            cached = await ensure_npb_pitchers_fresh(
                team_id, games, cached, expected=expected
            )
        panels_stale = False
        if cached and not force:
            from app.panel_freshness import panels_stale_for_league

            panels_stale = await panels_stale_for_league(
                "npb", team_id, cached.get("data") or {}
            )
        expected_mismatch = _expected_cache_mismatch(expected, cached)
        refreshing = await _await_matchup_refresh_if_forced(
            team_id=team_id,
            games=games,
            force=force,
            needs_refresh=needs_refresh or panels_stale,
            needs_timing_patch=False,
            is_refreshing_fn=npb_is_refreshing,
            refresh_header=refresh_npb_header,
            full_refresh_factory=lambda: refresh_npb_matchup(team_id, games, expected=expected),
            expected=expected,
            needs_full_rebuild=needs_full_rebuild,
            expected_mismatch=expected_mismatch,
            panels_stale=panels_stale,
        )
        if (force or expected_mismatch) and not is_cloud_lite():
            cached = get_npb_matchup(team_id, games)

        if cached and expected and not expected.matches_cache_entry(cached):
            payload = await _wrap_npb_matchup(
                team_id, cached, refreshing=True, games=games
            )
            payload["pickStale"] = True
            return payload

        if cached:
            return await _wrap_npb_matchup(
                team_id, cached, refreshing=refreshing, games=games
            )
        return loading_matchup_payload(team_id, cache_version=NPB_CACHE_VERSION)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NPB 資料錯誤: {exc}") from exc


@app.get("/api/npb/a-table")
async def api_npb_a_table(
    team_id: int = Query(..., ge=1, le=12, description="Selected NPB team ID"),
    force: bool = Query(False, description="Force refresh a-table"),
):
    try:
        entry = await ensure_npb_a_table(team_id, force=force)
        refreshing = force and npb_is_stale(entry["updatedAt"])
        return await asyncio.to_thread(
            wrap_npb_a_table_response, entry, refreshing=refreshing
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NPB a-table 錯誤: {exc}") from exc


@app.get("/api/cpbl/matchup")
async def api_cpbl_matchup(
    team_id: int = Query(..., ge=1, le=6, description="Selected CPBL team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30, description="Number of recent games"),
    force: bool = Query(False, description="Force refresh from CPBL.com.tw"),
    expected_date: str | None = Query(None),
    expected_away: int | None = Query(None),
    expected_home: int | None = Query(None),
):
    try:
        from app.cpbl_scheduler import refresh_matchup_header as refresh_cpbl_header

        expected = ExpectedMatchup.from_query(
            expected_date=expected_date,
            expected_away=expected_away,
            expected_home=expected_home,
        )
        cached = get_cpbl_matchup(team_id, games)
        needs_full_rebuild = force or _needs_full_matchup_rebuild(
            cached, league="cpbl", games=games
        )
        needs_refresh = (
            needs_full_rebuild
            or cached is None
            or cpbl_is_stale(cached["updatedAt"])
            or (expected is not None and not expected.matches_cache_entry(cached))
        )
        panels_stale = False
        if cached and not force:
            from app.panel_freshness import panels_stale_for_league

            panels_stale = await panels_stale_for_league(
                "cpbl", team_id, cached.get("data") or {}
            )
        expected_mismatch = _expected_cache_mismatch(expected, cached)
        refreshing = await _await_matchup_refresh_if_forced(
            team_id=team_id,
            games=games,
            force=force,
            needs_refresh=needs_refresh or panels_stale,
            needs_timing_patch=False,
            is_refreshing_fn=cpbl_is_refreshing,
            refresh_header=refresh_cpbl_header,
            full_refresh_factory=lambda: refresh_cpbl_matchup(team_id, games, expected=expected),
            expected=expected,
            needs_full_rebuild=needs_full_rebuild,
            expected_mismatch=expected_mismatch,
            panels_stale=panels_stale,
        )
        if (force or expected_mismatch) and not is_cloud_lite():
            cached = get_cpbl_matchup(team_id, games)

        if cached and expected and not expected.matches_cache_entry(cached):
            payload = await _wrap_cpbl_matchup(
                team_id, cached, refreshing=True, games=games
            )
            payload["pickStale"] = True
            return payload

        if cached:
            return await _wrap_cpbl_matchup(
                team_id, cached, refreshing=refreshing, games=games
            )
        return loading_matchup_payload(team_id, cache_version=CPBL_CACHE_VERSION)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CPBL 資料錯誤: {exc}") from exc


@app.get("/api/cpbl/lineup")
async def api_cpbl_lineup(
    team_id: int = Query(..., ge=1, le=6, description="Selected CPBL team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30),
    force: bool = Query(False, description="Rebuild lineups from CPBL boxscore"),
    expected_date: str | None = Query(None, description="Picked slate game date (YYYY-MM-DD)"),
    expected_away: int | None = Query(None, description="Picked away team ID"),
    expected_home: int | None = Query(None, description="Picked home team ID"),
):
    from app.cpbl_scheduler import refresh_matchup_header as refresh_cpbl_header

    expected = ExpectedMatchup.from_query(
        expected_date=expected_date,
        expected_away=expected_away,
        expected_home=expected_home,
    )
    cached = await _ensure_cached_matchup_for_expected(
        team_id=team_id,
        games=games,
        expected=expected,
        get_matchup_fn=get_cpbl_matchup,
        refresh_header_fn=refresh_cpbl_header,
        refresh_full_fn=refresh_cpbl_matchup,
        is_refreshing_fn=cpbl_is_refreshing,
    )
    if expected and _expected_cache_mismatch(expected, cached):
        force = True
    lineups = (cached.get("data") or {}).get("startingLineups") if cached else None
    matchup_meta = (cached.get("data") or {}).get("matchup") if cached else None
    # Require unique batting orders 1–9 — len>=9 alone used to keep mid-game dupes.
    if not force and not cpbl_lineups_need_rebuild(
        lineups,
        matchup_date=((matchup_meta or {}).get("date") or "")[:10],
        matchup_status=(matchup_meta or {}).get("status"),
    ):
        return lineups

    if not force and _lineups_have_card(lineups):
        async def _rebuild_cpbl_lineups() -> None:
            try:
                client = CpblClient()
                try:
                    matchup = await fetch_next_matchup(client, team_id, expected=expected)
                    if not matchup:
                        return
                    rebuilt = await fetch_matchup_starting_lineups(client, matchup)
                finally:
                    await client.close()
                entry = get_cpbl_matchup(team_id, games)
                if entry:
                    data = copy.deepcopy(entry["data"])
                    data["startingLineups"] = rebuilt
                    await store_cpbl_matchup(team_id, games, data)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("CPBL background lineup rebuild failed")

        _schedule(_rebuild_cpbl_lineups())
        return lineups

    client = CpblClient()
    try:
        matchup = await fetch_next_matchup(client, team_id, expected=expected)
        if not matchup:
            raise HTTPException(status_code=404, detail="找不到下一場比賽")
        lineups = await fetch_matchup_starting_lineups(client, matchup)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CPBL 打線錯誤: {exc}") from exc
    finally:
        await client.close()

    if cached:
        data = copy.deepcopy(cached["data"])
        data["startingLineups"] = lineups
        await store_cpbl_matchup(team_id, games, data)

    return lineups


@app.get("/api/mlb/lineup")
async def api_mlb_lineup(
    team_id: int = Query(..., description="Selected MLB team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30),
    force: bool = Query(False, description="Rebuild lineups from MLB boxscore"),
    expected_date: str | None = Query(None, description="Picked slate game date (YYYY-MM-DD)"),
    expected_away: int | None = Query(None, description="Picked away team ID"),
    expected_home: int | None = Query(None, description="Picked home team ID"),
):
    from app.scheduler import refresh_matchup, refresh_matchup_header

    expected = ExpectedMatchup.from_query(
        expected_date=expected_date,
        expected_away=expected_away,
        expected_home=expected_home,
    )
    cached = await _ensure_cached_matchup_for_expected(
        team_id=team_id,
        games=games,
        expected=expected,
        get_matchup_fn=get_matchup,
        refresh_header_fn=refresh_matchup_header,
        refresh_full_fn=refresh_matchup,
        is_refreshing_fn=mlb_is_refreshing,
    )
    if expected and _expected_cache_mismatch(expected, cached):
        force = True
    lineups = (cached.get("data") or {}).get("startingLineups") if cached else None
    matchup_meta = (cached.get("data") or {}).get("matchup") if cached else None
    matchup_date = ((matchup_meta or {}).get("date") or "")[:10]
    matchup_status = (matchup_meta or {}).get("status")
    trusted = mlb_lineups_trusted(
        lineups, matchup_date=matchup_date, matchup_status=matchup_status
    )
    if not force and not mlb_lineups_need_rebuild(
        lineups,
        matchup_date=matchup_date,
        matchup_status=matchup_status,
    ):
        return lineups

    if not force and _lineups_have_card(lineups) and trusted:
        async def _rebuild_mlb_lineups() -> None:
            try:
                async with httpx.AsyncClient(
                    timeout=60.0,
                    limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
                ) as client:
                    entry = get_matchup(team_id, games)
                    if not entry:
                        return
                    matchup = matchup_dict_from_cached_data(entry["data"])
                    if not matchup.get("gamePk"):
                        matchup = await fetch_mlb_next_matchup(
                            client, team_id, expected=expected
                        )
                    if not matchup:
                        return
                    rebuilt = await fetch_mlb_starting_lineups(client, matchup)
                entry = get_matchup(team_id, games)
                if entry:
                    data = copy.deepcopy(entry["data"])
                    data["startingLineups"] = rebuilt
                    await store_mlb_matchup(team_id, games, data)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("MLB background lineup rebuild failed")

        _schedule(_rebuild_mlb_lineups())
        return lineups

    async with httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
    ) as client:
        try:
            matchup = await fetch_mlb_next_matchup(client, team_id, expected=expected)
            if not matchup and cached and (cached.get("data") or {}).get("matchup", {}).get("gamePk"):
                matchup = matchup_dict_from_cached_data(cached["data"])
            if not matchup:
                raise HTTPException(status_code=404, detail="找不到下一場比賽")
            lineups = await fetch_mlb_starting_lineups(client, matchup)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"MLB 打線錯誤: {exc}") from exc

    if cached:
        data = copy.deepcopy(cached["data"])
        data["startingLineups"] = lineups
        await store_mlb_matchup(team_id, games, data)

    return lineups


@app.get("/api/npb/lineup")
async def api_npb_lineup(
    team_id: int = Query(..., ge=1, le=12, description="Selected NPB team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30),
    force: bool = Query(False, description="Rebuild lineups from npb.jp boxscore"),
    expected_date: str | None = Query(None, description="Picked slate game date (YYYY-MM-DD)"),
    expected_away: int | None = Query(None, description="Picked away team ID"),
    expected_home: int | None = Query(None, description="Picked home team ID"),
):
    from app.npb_scheduler import refresh_matchup_header as refresh_npb_header

    expected = ExpectedMatchup.from_query(
        expected_date=expected_date,
        expected_away=expected_away,
        expected_home=expected_home,
    )
    cached = await _ensure_cached_matchup_for_expected(
        team_id=team_id,
        games=games,
        expected=expected,
        get_matchup_fn=get_npb_matchup,
        refresh_header_fn=refresh_npb_header,
        refresh_full_fn=refresh_npb_matchup,
        is_refreshing_fn=npb_is_refreshing,
    )
    if expected and _expected_cache_mismatch(expected, cached):
        force = True
    lineups = (cached.get("data") or {}).get("startingLineups") if cached else None
    matchup_meta = (cached.get("data") or {}).get("matchup") if cached else None
    if not force and not npb_lineups_need_rebuild(
        lineups,
        matchup_date=((matchup_meta or {}).get("date") or "")[:10],
        matchup_status=(matchup_meta or {}).get("status"),
    ):
        if lineups:
            lineups = copy.deepcopy(lineups)
            localize_starting_lineups(lineups)
        return lineups

    if not force and _lineups_have_card(lineups):
        async def _rebuild_npb_lineups() -> None:
            try:
                client = NpbClient()
                try:
                    matchup = await fetch_npb_next_matchup(client, team_id, expected=expected)
                    if not matchup:
                        return
                    rebuilt = await fetch_npb_starting_lineups(client, matchup)
                finally:
                    await client.close()
                entry = get_npb_matchup(team_id, games)
                if entry:
                    data = copy.deepcopy(entry["data"])
                    data["startingLineups"] = rebuilt
                    await store_npb_matchup(team_id, games, data)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("NPB background lineup rebuild failed")

        _schedule(_rebuild_npb_lineups())
        lineups = copy.deepcopy(lineups)
        localize_starting_lineups(lineups)
        return lineups

    client = NpbClient()
    try:
        matchup = await fetch_npb_next_matchup(client, team_id, expected=expected)
        if not matchup:
            raise HTTPException(status_code=404, detail="找不到下一場比賽")
        lineups = await fetch_npb_starting_lineups(client, matchup)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NPB 打線錯誤: {exc}") from exc
    finally:
        await client.close()

    if cached:
        data = copy.deepcopy(cached["data"])
        data["startingLineups"] = lineups
        await store_npb_matchup(team_id, games, data)

    lineups = copy.deepcopy(lineups)
    localize_starting_lineups(lineups)
    return lineups


@app.get("/api/cpbl/a-table")
async def api_cpbl_a_table(
    team_id: int = Query(..., ge=1, le=6, description="Selected CPBL team ID"),
    force: bool = Query(False, description="Force refresh a-table"),
):
    try:
        entry = await ensure_cpbl_a_table(team_id, force=force)
        refreshing = force and cpbl_is_stale(entry["updatedAt"])
        return wrap_cpbl_a_table_response(entry, refreshing=refreshing)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CPBL a-table 錯誤: {exc}") from exc


@app.get("/api/cpbl/verify")
async def api_cpbl_verify(
    reset_schedule: bool = Query(False, description="Rebuild schedule before verifying"),
):
    """Automated regression check against stats.cpbl.com.tw (1–3 min)."""
    try:
        issues = await verify_cpbl(reset_schedule_cache=reset_schedule)
        return {
            "ok": not issues,
            "issueCount": len(issues),
            "issues": [{"check": item.check, "detail": item.detail} for item in issues],
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CPBL verify 錯誤: {exc}") from exc


@app.get("/api/health/data")
async def api_health_data(
    repair: bool = Query(False, description="Auto-repair CPBL wrong matchups"),
    repair_mlb: bool = Query(False, description="Auto-repair MLB cache issues"),
    repair_npb: bool = Query(False, description="Auto-repair NPB cache issues"),
):
    """Validate MLB/NPB/CPBL caches (wrong game, absurd avgs, thin panels)."""
    try:
        from app.data_validate import validate_all_caches

        report = await validate_all_caches(
            repair_cpbl=repair,
            repair_mlb=repair_mlb,
            repair_npb=repair_npb,
        )
        return report
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"資料驗證錯誤: {exc}") from exc


@app.get("/api/mlb/a-table")
async def api_mlb_a_table(
    team_id: int = Query(..., description="Selected MLB team ID"),
    force: bool = Query(False, description="Force refresh a-table"),
):
    try:
        entry = await ensure_mlb_a_table(team_id, force=force)
        refreshing = force and is_stale(entry["updatedAt"])
        return wrap_mlb_a_table_response(entry, refreshing=refreshing)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MLB a-table 錯誤: {exc}") from exc
