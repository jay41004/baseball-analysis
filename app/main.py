from contextlib import asynccontextmanager
import contextlib
import copy
from pathlib import Path

import asyncio

from fastapi import FastAPI, HTTPException, Query, Request
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
from app.npb_service import (
    NpbClient,
    fetch_matchup_starting_lineups as fetch_npb_starting_lineups,
    fetch_next_matchup as fetch_npb_next_matchup,
    fetch_npb_teams,
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
from app.cpbl_service import CpblClient, fetch_cpbl_teams, fetch_matchup_starting_lineups, fetch_next_matchup
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

BASE_DIR = Path(__file__).resolve().parent.parent


def _schedule(coro) -> None:
    # Always schedule on-demand refresh (force / stale / cache miss).
    # CLOUD_LITE only disables keepalive + full warm-all loops, not user refresh.
    asyncio.create_task(coro)


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

    if not is_refreshing_table(team_id):
        _schedule(refresh_table(team_id))
    return payload


async def _wrap_npb_matchup(team_id: int, entry: dict, *, refreshing: bool) -> dict:
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


def _wrap_cpbl_matchup(team_id: int, entry: dict, *, refreshing: bool) -> dict:
    payload = wrap_cpbl_matchup_response(entry, refreshing=refreshing)
    return _attach_a_table(
        payload,
        team_id,
        get_table=get_cpbl_a_table,
        is_refreshing_table=cpbl_is_refreshing_a_table,
        refresh_table=refresh_cpbl_a_table,
    )


def _wrap_mlb_matchup(team_id: int, entry: dict, *, refreshing: bool) -> dict:
    payload = wrap_matchup_response(entry, refreshing=refreshing)
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
            await asyncio.to_thread(load_mlb_disk)
            await asyncio.sleep(1)
            await asyncio.to_thread(load_npb_disk)
            await asyncio.sleep(1)
            await asyncio.to_thread(load_cpbl_disk)
            await start_cache_services(skip_load=True)
            await start_npb_cache_services(skip_load=True)
            await start_cpbl_cache_services(skip_load=True)
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


@app.get("/api/teams")
async def api_teams():
    return await fetch_teams()


@app.get("/api/npb/teams")
async def api_npb_teams():
    return await fetch_npb_teams()


@app.get("/api/cpbl/teams")
async def api_cpbl_teams():
    return await fetch_cpbl_teams()


@app.get("/api/matchup")
async def api_matchup(
    team_id: int = Query(..., description="Selected MLB team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30, description="Number of recent games"),
    force: bool = Query(False, description="Force refresh from MLB API"),
):
    try:
        cached = get_matchup(team_id, games)
        needs_refresh = force or cached is None or is_stale(cached["updatedAt"])

        if needs_refresh and not mlb_is_refreshing(team_id, games):
            # Background only — awaiting full MLB rebuild OOMs Render free tier.
            _schedule(refresh_matchup(team_id, games))

        if cached:
            return _wrap_mlb_matchup(
                team_id, cached, refreshing=needs_refresh or mlb_is_refreshing(team_id, games)
            )
        return loading_matchup_payload(team_id, cache_version=MLB_CACHE_VERSION)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MLB API error: {exc}") from exc


@app.get("/api/npb/matchup")
async def api_npb_matchup(
    team_id: int = Query(..., ge=1, le=12, description="Selected NPB team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30, description="Number of recent games"),
    force: bool = Query(False, description="Force refresh from NPB.jp"),
):
    try:
        cached = get_npb_matchup(team_id, games)
        needs_refresh = force or cached is None or npb_is_stale(cached["updatedAt"])

        if needs_refresh and not npb_is_refreshing(team_id, games):
            _schedule(refresh_npb_matchup(team_id, games))

        if cached:
            return await _wrap_npb_matchup(
                team_id, cached, refreshing=needs_refresh or npb_is_refreshing(team_id, games)
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
):
    try:
        cached = get_cpbl_matchup(team_id, games)
        needs_refresh = (
            force
            or cached is None
            or cpbl_is_stale(cached["updatedAt"])
            or cpbl_cache_needs_upgrade(cached)
        )

        if needs_refresh and not cpbl_is_refreshing(team_id, games):
            _schedule(refresh_cpbl_matchup(team_id, games))

        if cached:
            return _wrap_cpbl_matchup(
                team_id, cached, refreshing=needs_refresh or cpbl_is_refreshing(team_id, games)
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
):
    cached = get_cpbl_matchup(team_id, games)
    lineups = (cached.get("data") or {}).get("startingLineups") if cached else None
    away_count = len((lineups or {}).get("away", {}).get("batters") or [])
    home_count = len((lineups or {}).get("home", {}).get("batters") or [])
    # Official firstSno is often incomplete (6–7 batters). Prefer a full 9-man card.
    if away_count >= 9 and home_count >= 9:
        return lineups

    client = CpblClient()
    try:
        matchup = await fetch_next_matchup(client, team_id)
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
):
    cached = get_matchup(team_id, games)
    lineups = (cached.get("data") or {}).get("startingLineups") if cached else None
    away_count = len((lineups or {}).get("away", {}).get("batters") or [])
    home_count = len((lineups or {}).get("home", {}).get("batters") or [])
    if away_count or home_count:
        return lineups

    async with httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
    ) as client:
        try:
            matchup = await fetch_mlb_next_matchup(client, team_id)
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
):
    cached = get_npb_matchup(team_id, games)
    lineups = (cached.get("data") or {}).get("startingLineups") if cached else None
    away_count = len((lineups or {}).get("away", {}).get("batters") or [])
    home_count = len((lineups or {}).get("home", {}).get("batters") or [])
    if away_count or home_count:
        return lineups

    client = NpbClient()
    try:
        matchup = await fetch_npb_next_matchup(client, team_id)
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
