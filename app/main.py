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
    wrap_a_table_response as wrap_mlb_a_table_response,
    wrap_matchup_response,
)
from app.mlb_service import fetch_teams
from app.npb_cache import (
    CACHE_VERSION as NPB_CACHE_VERSION,
    cache_needs_upgrade as npb_cache_needs_upgrade,
    cached_team_count as npb_cached_team_count,
    get_a_table as get_npb_a_table,
    get_matchup as get_npb_matchup,
    is_stale as npb_is_stale,
    wrap_a_table_response as wrap_npb_a_table_response,
    wrap_matchup_response as wrap_npb_matchup_response,
)
from app.npb_service import fetch_npb_teams
from app.loading_response import loading_matchup_payload
from app.npb_scheduler import refresh_matchup as refresh_npb_matchup
from app.npb_scheduler import is_refreshing as npb_is_refreshing
from app.npb_scheduler import is_refreshing_a_table as npb_is_refreshing_a_table
from app.npb_scheduler import is_warming_all as npb_is_warming_all
from app.npb_scheduler import refresh_a_table as refresh_npb_a_table
from app.npb_scheduler import refresh_all_matchups as refresh_all_npb_matchups
from app.npb_scheduler import start_npb_cache_services
from app.cloud_keepalive import cloud_keepalive_loop
from app.scheduler import refresh_matchup, is_refreshing as mlb_is_refreshing
from app.scheduler import is_refreshing_a_table as mlb_is_refreshing_a_table
from app.scheduler import is_warming_all as mlb_is_warming_all
from app.scheduler import refresh_a_table as refresh_mlb_a_table
from app.scheduler import refresh_all_matchups as refresh_all_mlb_matchups
from app.scheduler import start_cache_services

BASE_DIR = Path(__file__).resolve().parent.parent


def _attach_a_table(
    payload: dict,
    team_id: int,
    *,
    get_table,
    is_refreshing_table,
    refresh_table,
) -> dict:
    entry = get_table(team_id)
    if entry and entry.get("data"):
        merged = copy.deepcopy(payload)
        merged["aTable"] = copy.deepcopy(entry["data"])
        return merged
    if not is_refreshing_table(team_id):
        asyncio.create_task(refresh_table(team_id))
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
    await start_cache_services()
    await start_npb_cache_services()
    keepalive_task = asyncio.create_task(cloud_keepalive_loop())
    try:
        yield
    finally:
        keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
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
    return {
        "mlbCacheVersion": MLB_CACHE_VERSION,
        "npbCacheVersion": NPB_CACHE_VERSION,
        "mlbTeamsCached": mlb_cached,
        "npbTeamsCached": npb_cached,
        "mlbTeamsTotal": 30,
        "npbTeamsTotal": 12,
        "cacheReady": mlb_cached >= 30 and npb_cached >= 12,
        "warming": mlb_is_warming_all() or npb_is_warming_all(),
    }


@app.get("/api/warmup")
async def api_warmup():
    """Keep server awake and refresh all team caches in the background."""
    if not mlb_is_warming_all():
        asyncio.create_task(refresh_all_mlb_matchups(DEFAULT_GAMES))
    if not npb_is_warming_all():
        asyncio.create_task(refresh_all_npb_matchups(DEFAULT_GAMES))
    return {
        "status": "warming",
        "mlbTeamsCached": mlb_cached_team_count(DEFAULT_GAMES),
        "npbTeamsCached": npb_cached_team_count(DEFAULT_GAMES),
        "warming": True,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/npb", response_class=HTMLResponse)
async def npb_index(request: Request):
    return templates.TemplateResponse("npb.html", {"request": request})


@app.get("/api/teams")
async def api_teams():
    return await fetch_teams()


@app.get("/api/npb/teams")
async def api_npb_teams():
    return await fetch_npb_teams()


@app.get("/api/matchup")
async def api_matchup(
    team_id: int = Query(..., description="Selected MLB team ID"),
    games: int = Query(DEFAULT_GAMES, ge=1, le=30, description="Number of recent games"),
    force: bool = Query(False, description="Force refresh from MLB API"),
):
    try:
        cached = get_matchup(team_id, games)

        if force:
            if not mlb_is_refreshing(team_id, games):
                asyncio.create_task(refresh_matchup(team_id, games))
            if cached:
                return _wrap_mlb_matchup(team_id, cached, refreshing=True)
            return loading_matchup_payload(team_id, cache_version=MLB_CACHE_VERSION)

        if cached:
            needs_refresh = is_stale(cached["updatedAt"])
            if needs_refresh and not mlb_is_refreshing(team_id, games):
                asyncio.create_task(refresh_matchup(team_id, games))
                return _wrap_mlb_matchup(team_id, cached, refreshing=True)
            return _wrap_mlb_matchup(team_id, cached, refreshing=False)

        if not mlb_is_refreshing(team_id, games):
            asyncio.create_task(refresh_matchup(team_id, games))
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

        if force:
            if not npb_is_refreshing(team_id, games):
                asyncio.create_task(refresh_npb_matchup(team_id, games))
            if cached:
                return await _wrap_npb_matchup(team_id, cached, refreshing=True)
            return loading_matchup_payload(team_id, cache_version=NPB_CACHE_VERSION)

        if cached:
            needs_refresh = npb_is_stale(cached["updatedAt"])
            if needs_refresh and not npb_is_refreshing(team_id, games):
                asyncio.create_task(refresh_npb_matchup(team_id, games))
                return await _wrap_npb_matchup(team_id, cached, refreshing=True)
            return await _wrap_npb_matchup(team_id, cached, refreshing=False)

        if not npb_is_refreshing(team_id, games):
            asyncio.create_task(refresh_npb_matchup(team_id, games))
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
        cached = get_npb_a_table(team_id)
        if force:
            if not npb_is_refreshing_a_table(team_id):
                asyncio.create_task(refresh_npb_a_table(team_id))
            if cached:
                return await asyncio.to_thread(
                    wrap_npb_a_table_response, cached, refreshing=True
                )
            return {"away": None, "home": None, "loading": True, "refreshing": True}

        if cached:
            if npb_is_stale(cached["updatedAt"]) and not npb_is_refreshing_a_table(team_id):
                asyncio.create_task(refresh_npb_a_table(team_id))
                return await asyncio.to_thread(
                    wrap_npb_a_table_response, cached, refreshing=True
                )
            return await asyncio.to_thread(wrap_npb_a_table_response, cached)

        if not npb_is_refreshing_a_table(team_id):
            asyncio.create_task(refresh_npb_a_table(team_id))
        return {"away": None, "home": None, "loading": True, "refreshing": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NPB a-table 錯誤: {exc}") from exc


@app.get("/api/mlb/a-table")
async def api_mlb_a_table(
    team_id: int = Query(..., description="Selected MLB team ID"),
    force: bool = Query(False, description="Force refresh a-table"),
):
    try:
        cached = get_mlb_a_table(team_id)
        if force:
            if not mlb_is_refreshing_a_table(team_id):
                asyncio.create_task(refresh_mlb_a_table(team_id))
            if cached:
                return wrap_mlb_a_table_response(cached, refreshing=True)
            return {"away": None, "home": None, "loading": True, "refreshing": True}

        if cached:
            if is_stale(cached["updatedAt"]) and not mlb_is_refreshing_a_table(team_id):
                asyncio.create_task(refresh_mlb_a_table(team_id))
                return wrap_mlb_a_table_response(cached, refreshing=True)
            return wrap_mlb_a_table_response(cached)

        if not mlb_is_refreshing_a_table(team_id):
            asyncio.create_task(refresh_mlb_a_table(team_id))
        return {"away": None, "home": None, "loading": True, "refreshing": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MLB a-table 錯誤: {exc}") from exc
