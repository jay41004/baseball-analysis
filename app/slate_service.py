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


def _ymd_minus(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) - timedelta(days=days)).isoformat()


def _mlb_day_bucket(official: str, today: str, tomorrow: str) -> str | None:
    """台灣欄位日期 −1 天 = 美國 officialDate。

    例：台灣今天 9/7、明天 9/8 → 今天欄美國 9/6、明天欄美國 9/7。
    """
    if not official:
        return None
    if official == _ymd_minus(today, 1):
        return "today"
    if official == _ymd_minus(tomorrow, 1):
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
        out[key].sort(
            key=lambda g: (
                g.get("date") or "",
                g.get("timeTaiwan") or "99:99",
                g.get("awayName") or "",
            )
        )
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
    from app.mlb_service import MLB_BASE, UPCOMING_GAME_STATES
    from app.team_names import team_name_zh

    today, tomorrow = _today_tomorrow()
    # Taiwan today/tomorrow: bucket by Taiwan wall-clock date of first pitch.
    start = date.fromisoformat(today) - timedelta(days=2)
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
            official = str(game.get("officialDate") or day.get("date") or "")[:10]
            timing = format_matchup_timing(
                game_date_iso,
                venue_raw=str((game.get("venue") or {}).get("name") or ""),
                home_team_id=home_id,
                official_date=official or None,
            )
            tw_date = timing["date"]
            # 台灣今天欄→美國昨天；台灣明天欄→美國今天（例：台 9/7、9/8 → 美 9/6、9/7）。
            bucket = _mlb_day_bucket(official, today, tomorrow)
            if not bucket:
                continue
            away_p = game["teams"]["away"].get("probablePitcher") or {}
            home_p = game["teams"]["home"].get("probablePitcher") or {}
            entry = _slate_entry(
                league="mlb",
                game_date=official,
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
            entry["taiwanDate"] = tw_date
            entry["officialDate"] = official
            entry["gamePk"] = game.get("gamePk")
            entry["_bucket"] = bucket
            rows.append(entry)
    return _bucket_games(rows, today, tomorrow)


async def resolve_mlb_slate_pick(team_id: int) -> dict[str, Any] | None:
    """Today's slate first, then tomorrow — US officialDate row for this team."""
    bucket = await fetch_mlb_slate()
    for key in ("today", "tomorrow"):
        for game in bucket.get(key) or []:
            if team_id in {int(game["awayTeamId"]), int(game["homeTeamId"])}:
                return game
    return None


def expected_from_slate_row(row: dict[str, Any]) -> "ExpectedMatchup":
    from app.matchup_pick import ExpectedMatchup

    return ExpectedMatchup(
        date=str(row.get("date") or "")[:10] or None,
        away_id=int(row["awayTeamId"]),
        home_id=int(row["homeTeamId"]),
        game_pk=int(row["gamePk"]) if row.get("gamePk") else None,
    )


async def align_expected_with_slate(
    team_id: int, expected: "ExpectedMatchup | None"
) -> "ExpectedMatchup | None":
    """Match by gamePk or team pair on today's/tomorrow slate (US officialDate)."""
    from app.matchup_pick import ExpectedMatchup

    bucket = await fetch_mlb_slate()
    rows = (bucket.get("today") or []) + (bucket.get("tomorrow") or [])
    if not rows:
        return expected

    if expected and expected.game_pk:
        for row in rows:
            if int(row.get("gamePk") or 0) == int(expected.game_pk):
                return expected_from_slate_row(row)

    if expected and expected.away_id and expected.home_id:
        for row in rows:
            if {int(row["awayTeamId"]), int(row["homeTeamId"])} == {
                int(expected.away_id),
                int(expected.home_id),
            }:
                return expected_from_slate_row(row)

    row = await resolve_mlb_slate_pick(team_id)
    return expected_from_slate_row(row) if row else expected


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
