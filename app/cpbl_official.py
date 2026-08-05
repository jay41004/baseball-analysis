"""CPBL official site helpers (homepage game detail API)."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx

from app.cpbl_teams import team_by_code

# Prefer non-www origin; www CDN nodes frequently break schedule/game APIs.
CPBL_BASE = "https://cpbl.com.tw"
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

    # Mirror cpbl_service: PresentStatus alone is not Final (live + future shells).
    today = date.today().isoformat()
    has_decision = any(
        str(raw.get(k) or "").strip() not in {"", "0"}
        for k in (
            "WinningPitcherAcnt",
            "WinningPitcherName",
            "GameDateTimeE",
            "GameDuringTime",
            "MvpAcnt",
        )
    )
    is_play_ball = str(raw.get("IsPlayBall") or "").strip() in {"1", "Y", "y", "true", "True"}
    away_score = raw.get("VisitingScore")
    home_score = raw.get("HomeScore")
    try:
        scored = int(away_score or 0) != 0 or int(home_score or 0) != 0
    except (TypeError, ValueError):
        scored = False

    if str(raw.get("IsGameStop") or "").strip() in {"1", "Y", "y", "true", "True"}:
        status = "Cancelled"
    elif iso_date and iso_date > today:
        status = "Scheduled"
    elif has_decision:
        status = "Final"
    elif is_play_ball or (iso_date == today and scored):
        status = "In Progress"
    elif (
        str(raw.get("PresentStatus")) == "1"
        and away_score is not None
        and home_score is not None
        and iso_date
        and iso_date < today
    ):
        status = "Final"
    else:
        status = "Scheduled"

    return {
        "gameSno": raw.get("GameSno"),
        "year": year or date.today().year,
        "date": iso_date,
        "awayTeamId": away_team["id"],
        "homeTeamId": home_team["id"],
        "awayNameZh": away_team["nameZh"],
        "homeNameZh": home_team["nameZh"],
        "awayScore": away_score,
        "homeScore": home_score,
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
    from datetime import timedelta

    today = date.today()
    today_iso = today.isoformat()
    horizon_iso = (today + timedelta(days=7)).isoformat()
    dates: set[str] = set()
    for game in games:
        if game.get("awayProbablePitcher") and game.get("homeProbablePitcher"):
            continue
        iso_date = game.get("date") or ""
        if today_iso <= iso_date <= horizon_iso:
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
