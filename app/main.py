from contextlib import asynccontextmanager
from pathlib import Path

import asyncio

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.cache import (
    CACHE_VERSION as MLB_CACHE_VERSION,
    DEFAULT_GAMES,
    get_matchup,
    is_stale,
    store_matchup,
    wrap_matchup_response,
)
from app.mlb_service import analyze_matchup, fetch_teams
from app.npb_cache import (
    CACHE_VERSION as NPB_CACHE_VERSION,
    get_matchup as get_npb_matchup,
    is_stale as npb_is_stale,
    store_matchup as store_npb_matchup,
    wrap_matchup_response as wrap_npb_matchup_response,
)
from app.npb_service import analyze_matchup as analyze_npb_matchup
from app.npb_service import fetch_npb_teams
from app.npb_scheduler import refresh_matchup as refresh_npb_matchup
from app.npb_scheduler import start_npb_cache_services
from app.scheduler import refresh_matchup, start_cache_services

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_cache_services()
    await start_npb_cache_services()
    yield


app = FastAPI(title="棒球前五局分析", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/meta")
async def api_meta():
    return {
        "mlbCacheVersion": MLB_CACHE_VERSION,
        "npbCacheVersion": NPB_CACHE_VERSION,
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

        if cached and not force:
            if is_stale(cached["updatedAt"]):
                asyncio.create_task(refresh_matchup(team_id, games))
                return wrap_matchup_response(cached, refreshing=True)
            return wrap_matchup_response(cached)

        if cached and force:
            data = await analyze_matchup(team_id, games)
            entry = await store_matchup(team_id, games, data)
            return wrap_matchup_response(entry, from_cache=False)

        data = await analyze_matchup(team_id, games)
        entry = await store_matchup(team_id, games, data)
        return wrap_matchup_response(entry, from_cache=False)
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

        if cached and not force:
            if npb_is_stale(cached["updatedAt"]):
                asyncio.create_task(refresh_npb_matchup(team_id, games))
                return wrap_npb_matchup_response(cached, refreshing=True)
            return wrap_npb_matchup_response(cached)

        if cached and force:
            data = await analyze_npb_matchup(team_id, games)
            entry = await store_npb_matchup(team_id, games, data)
            return wrap_npb_matchup_response(entry, from_cache=False)

        data = await analyze_npb_matchup(team_id, games)
        entry = await store_npb_matchup(team_id, games, data)
        return wrap_npb_matchup_response(entry, from_cache=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NPB 資料錯誤: {exc}") from exc
