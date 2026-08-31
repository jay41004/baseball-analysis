"""Today / tomorrow matchup slates (read-only, no analysis)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

TPE = timezone(timedelta(hours=8))
JST = timezone(timedelta(hours=9))


def _today_tomorrow() -> tuple[str, str]:
    today = datetime.now(TPE).date()
    tomorrow = today + timedelta(days=1)
    return today.isoformat(), tomorrow.isoformat()


def _day_bucket(game_date: str, today: str, tomorrow: str) -> str | None:
    if game_date == today:
        return "today"
    if game_date == tomorrow:
        return "tomorrow"
    return None


def _format_time_taiwan(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(TPE).strftime("%H:%M")
    except ValueError:
        return ""


def _format_time_jst_from_hhmm(hhmm: str | None) -> str:
    if not hhmm or ":" not in hhmm:
        return ""
    try:
        hour, minute = hhmm.split(":", 1)
        dt = datetime.now(JST).replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0
        )
        return dt.astimezone(TPE).strftime("%H:%M")
    except ValueError:
        return ""


def _slate_entry(
    *,
    league: str,
    game_date: str,
    away_team_id: int,
    home_team_id: int,
    away_name: str,
    home_name: str,
    status: str,
    stadium: str = "",
    away_pitcher: str | None = None,
    home_pitcher: str | None = None,
    time_taiwan: str = "",
    time_local: str = "",
) -> dict[str, Any]:
    return {
        "league": league,
        "date": game_date,
        "awayTeamId": away_team_id,
        "homeTeamId": home_team_id,
        "awayName": away_name,
        "homeName": home_name,
        "awayPitcher": away_pitcher or "",
        "homePitcher": home_pitcher or "",
        "status": status or "Scheduled",
        "stadium": stadium or "",
        "timeTaiwan": time_taiwan,
        "timeLocal": time_local,
        "analysisUrl": f"/{'' if league == 'mlb' else league}?team={away_team_id}",
    }


def _bucket_games(games: list[dict[str, Any]], today: str, tomorrow: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"today": [], "tomorrow": []}
    for game in games:
        bucket = game.pop("_bucket", None)
        if bucket == "today":
            out["today"].append(game)
        elif bucket == "tomorrow":
            out["tomorrow"].append(game)
    for key in ("today", "tomorrow"):
        out[key].sort(key=lambda g: (g.get("timeTaiwan") or "99:99", g.get("awayName") or ""))
    return out


async def fetch_npb_slate() -> dict[str, list[dict[str, Any]]]:
    from app.npb_service import NpbClient

    today, tomorrow = _today_tomorrow()
    client = NpbClient()
    try:
        schedule = await client.fetch_schedule(months_back=1)
    finally:
        await client.close()

    rows: list[dict[str, Any]] = []
    for game in schedule:
        game_date = str(game.get("date") or "")[:10]
        bucket = _day_bucket(game_date, today, tomorrow)
        if not bucket:
            continue
        status = str(game.get("status") or "Scheduled")
        if status == "Final":
            continue
        start = str(game.get("startTime") or "")
        entry = _slate_entry(
            league="npb",
            game_date=game_date,
            away_team_id=int(game["awayTeamId"]),
            home_team_id=int(game["homeTeamId"]),
            away_name=str(game.get("awayNameZh") or ""),
            home_name=str(game.get("homeNameZh") or ""),
            status=status,
            stadium=str(game.get("stadium") or "").split()[0] if game.get("stadium") else "",
            away_pitcher=game.get("awayProbablePitcher"),
            home_pitcher=game.get("homeProbablePitcher"),
            time_local=start,
            time_taiwan=_format_time_jst_from_hhmm(start),
        )
        entry["_bucket"] = bucket
        rows.append(entry)
    return _bucket_games(rows, today, tomorrow)


async def fetch_cpbl_slate() -> dict[str, list[dict[str, Any]]]:
    from app.cpbl_service import CpblClient
    from app.cpbl_teams import team_zh

    today, tomorrow = _today_tomorrow()
    client = CpblClient()
    try:
        schedule = await client.fetch_schedule_pool()
    finally:
        await client.close()

    rows: list[dict[str, Any]] = []
    for game in schedule:
        game_date = str(game.get("date") or "")[:10]
        bucket = _day_bucket(game_date, today, tomorrow)
        if not bucket:
            continue
        status = str(game.get("status") or "Scheduled")
        if status in {"Final", "Cancelled"}:
            continue
        iso = game.get("gameDate") or f"{game_date}T18:00:00+08:00"
        entry = _slate_entry(
            league="cpbl",
            game_date=game_date,
            away_team_id=int(game["awayTeamId"]),
            home_team_id=int(game["homeTeamId"]),
            away_name=str(game.get("awayNameZh") or team_zh(int(game["awayTeamId"]))),
            home_name=str(game.get("homeNameZh") or team_zh(int(game["homeTeamId"]))),
            status=status,
            stadium=str(game.get("stadium") or ""),
            away_pitcher=game.get("awayProbablePitcher"),
            home_pitcher=game.get("homeProbablePitcher"),
            time_taiwan=_format_time_taiwan(str(iso)),
            time_local="",
        )
        entry["_bucket"] = bucket
        rows.append(entry)
    return _bucket_games(rows, today, tomorrow)


async def fetch_mlb_slate() -> dict[str, list[dict[str, Any]]]:
    from app.mlb_display import format_matchup_timing
    from app.mlb_service import MLB_BASE, UPCOMING_GAME_STATES, mlb_schedule_start
    from app.team_names import team_name_zh

    today, tomorrow = _today_tomorrow()
    start = mlb_schedule_start()
    end = date.fromisoformat(tomorrow) + timedelta(days=1)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{MLB_BASE}/schedule",
            params={
                "sportId": 1,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "gameType": "R",
                "hydrate": "probablePitcher",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    rows: list[dict[str, Any]] = []
    for day in data.get("dates", []):
        for game in day.get("games", []):
            state = (game.get("status") or {}).get("abstractGameState") or ""
            if state not in UPCOMING_GAME_STATES and state != "Final":
                continue
            if state == "Final":
                continue
            away = game["teams"]["away"]["team"]
            home = game["teams"]["home"]["team"]
            game_date_iso = str(game.get("gameDate") or "")
            home_id = int(home["id"])
            timing = format_matchup_timing(
                game_date_iso,
                venue_raw=str((game.get("venue") or {}).get("name") or ""),
                home_team_id=home_id,
                official_date=str(game.get("officialDate") or day.get("date") or "")[:10] or None,
            )
            tw_date = timing["date"]
            bucket = _day_bucket(tw_date, today, tomorrow)
            if not bucket:
                continue
            away_p = game["teams"]["away"].get("probablePitcher") or {}
            home_p = game["teams"]["home"].get("probablePitcher") or {}
            entry = _slate_entry(
                league="mlb",
                game_date=tw_date,
                away_team_id=int(away["id"]),
                home_team_id=home_id,
                away_name=team_name_zh(team_id=away["id"], english_name=away.get("name")),
                home_name=team_name_zh(team_id=home["id"], english_name=home.get("name")),
                status=str((game.get("status") or {}).get("detailedState") or state),
                stadium=timing["stadium"],
                away_pitcher=away_p.get("fullName"),
                home_pitcher=home_p.get("fullName"),
                time_taiwan=timing["timeTaiwan"],
                time_local=timing["timeLocal"],
            )
            entry["_bucket"] = bucket
            rows.append(entry)
    return _bucket_games(rows, today, tomorrow)


_SLATE_FETCHERS = {
    "npb": fetch_npb_slate,
    "cpbl": fetch_cpbl_slate,
    "mlb": fetch_mlb_slate,
}


async def fetch_league_slate(league: str) -> dict[str, Any]:
    key = (league or "").strip().lower()
    fetcher = _SLATE_FETCHERS.get(key)
    if not fetcher:
        raise ValueError(f"unknown league: {league}")
    today, tomorrow = _today_tomorrow()
    bucket = await fetcher()
    return {
        "league": key,
        "today": today,
        "tomorrow": tomorrow,
        "todayGames": bucket["today"],
        "tomorrowGames": bucket["tomorrow"],
        "generatedAt": datetime.now(timezone.utc).astimezone(TPE).isoformat(),
    }


async def fetch_all_slates() -> dict[str, Any]:
    today, tomorrow = _today_tomorrow()
    npb, cpbl, mlb = await asyncio.gather(
        fetch_npb_slate(),
        fetch_cpbl_slate(),
        fetch_mlb_slate(),
    )
    return {
        "today": today,
        "tomorrow": tomorrow,
        "npb": npb,
        "cpbl": cpbl,
        "mlb": mlb,
        "generatedAt": datetime.now(timezone.utc).astimezone(TPE).isoformat(),
    }
