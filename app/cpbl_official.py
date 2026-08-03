"""CPBL official site helpers (homepage game detail API)."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx

from app.cpbl_teams import team_by_code

CPBL_BASE = "https://www.cpbl.com.tw"
KIND_CODE = "A"

CPBL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": f"{CPBL_BASE}/",
    "Origin": CPBL_BASE,
    "X-Requested-With": "XMLHttpRequest",
}

_CSRF_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')


def _parse_game_date(raw: str | None) -> str:
    if not raw:
        return ""
    return str(raw).strip().replace("/", "-")[:10]


def _pitcher_name(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = (raw.get(key) or "").strip()
        if value:
            return value
    return None


def _normalize_home_game(raw: dict[str, Any]) -> dict[str, Any] | None:
    away_team = team_by_code(raw.get("VisitingTeamCode"))
    home_team = team_by_code(raw.get("HomeTeamCode"))
    if not away_team or not home_team:
        return None

    iso_date = _parse_game_date(raw.get("PreExeDate") or raw.get("GameDate"))
    year = raw.get("Year")
    if not year and iso_date:
        year = int(iso_date[:4])

    away_pitcher = _pitcher_name(
        raw, "VisitingPitcherName", "VisitingFirstMover", "VisitingPitcher"
    )
    home_pitcher = _pitcher_name(raw, "HomePitcherName", "HomeFirstMover", "HomePitcher")

    status = "Scheduled"
    today = date.today().isoformat()
    scores_ready = (
        raw.get("VisitingScore") is not None and raw.get("HomeScore") is not None
    )
    if str(raw.get("PresentStatus")) == "1" and scores_ready:
        status = "Final"
    elif iso_date and iso_date > today:
        status = "Scheduled"

    return {
        "gameSno": raw.get("GameSno"),
        "year": year or date.today().year,
        "date": iso_date,
        "awayTeamId": away_team["id"],
        "homeTeamId": home_team["id"],
        "awayNameZh": away_team["nameZh"],
        "homeNameZh": home_team["nameZh"],
        "awayScore": raw.get("VisitingScore"),
        "homeScore": raw.get("HomeScore"),
        "status": status,
        "stadium": raw.get("FieldAbbe") or "",
        "awayProbablePitcher": away_pitcher,
        "homeProbablePitcher": home_pitcher,
    }


async def fetch_home_games_for_date(
    http: httpx.AsyncClient, game_date: date, *, kind_code: str = KIND_CODE
) -> list[dict[str, Any]]:
    """Fetch scheduled/final games with probable pitchers from cpbl.com.tw homepage API."""
    try:
        page = await http.get(f"{CPBL_BASE}/", headers=CPBL_HEADERS)
        page.raise_for_status()
    except httpx.HTTPError:
        return []

    token_match = _CSRF_RE.search(page.text)
    if not token_match:
        return []
    token = token_match.group(1)

    data = {
        "__RequestVerificationToken": token,
        "GameSno": "",
        "KindCode": kind_code,
        "GameDate": game_date.strftime("%Y/%m/%d"),
    }
    try:
        response = await http.post(
            f"{CPBL_BASE}/home/getdetaillist",
            data=data,
            headers={**CPBL_HEADERS, "RequestVerificationToken": token},
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return []

    if response.status_code != 200:
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    if not payload.get("Success"):
        return []

    try:
        raw_games = json.loads(payload.get("GameADetailJson") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []

    games: list[dict[str, Any]] = []
    for raw in raw_games:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_home_game(raw)
        if normalized:
            games.append(normalized)
    return games


async def enrich_schedule_probable_pitchers(
    http: httpx.AsyncClient, games: list[dict[str, Any]]
) -> None:
    """Fill missing probable pitchers from the official homepage API."""
    today = date.today().isoformat()
    dates: set[str] = set()
    for game in games:
        if game.get("awayProbablePitcher") and game.get("homeProbablePitcher"):
            continue
        iso_date = game.get("date") or ""
        if iso_date >= today:
            dates.add(iso_date)

    official_by_sno: dict[Any, dict[str, Any]] = {}
    for iso_date in sorted(dates):
        try:
            day = date.fromisoformat(iso_date)
        except ValueError:
            continue
        for official in await fetch_home_games_for_date(http, day):
            game_sno = official.get("gameSno")
            if game_sno is not None:
                official_by_sno[game_sno] = official

    for game in games:
        official = official_by_sno.get(game.get("gameSno"))
        if not official:
            continue
        if not game.get("awayProbablePitcher") and official.get("awayProbablePitcher"):
            game["awayProbablePitcher"] = official["awayProbablePitcher"]
        if not game.get("homeProbablePitcher") and official.get("homeProbablePitcher"):
            game["homeProbablePitcher"] = official["homeProbablePitcher"]
