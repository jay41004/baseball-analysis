"""MLB venue + Taiwan-time helpers for matchup headers and slates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.team_names import team_name_zh

TPE = timezone(timedelta(hours=8))

# Home ballpark IANA timezone (for local first-pitch display, like NPB stadium time).
_TEAM_HOME_TZ: dict[int, str] = {
    108: "America/Los_Angeles",
    109: "America/Phoenix",
    110: "America/New_York",
    111: "America/New_York",
    112: "America/Chicago",
    113: "America/New_York",
    114: "America/New_York",
    115: "America/Denver",
    116: "America/Detroit",
    117: "America/Chicago",
    118: "America/Chicago",
    119: "America/Los_Angeles",
    120: "America/New_York",
    121: "America/New_York",
    133: "America/Los_Angeles",
    134: "America/New_York",
    135: "America/Los_Angeles",
    136: "America/Los_Angeles",
    137: "America/Los_Angeles",
    138: "America/Chicago",
    139: "America/New_York",
    140: "America/Chicago",
    141: "America/Toronto",
    142: "America/Chicago",
    143: "America/New_York",
    144: "America/New_York",
    145: "America/Chicago",
    146: "America/New_York",
    147: "America/New_York",
    158: "America/Chicago",
}

# Known venues (substring match for "UNIQLO Field at Dodger Stadium", etc.)
_VENUE_ZH: dict[str, str] = {
    "Busch Stadium": "紅雀主場",
    "Dodger Stadium": "道奇主場",
    "Yankee Stadium": "洋基主場",
    "Fenway Park": "紅襪主場",
    "Wrigley Field": "小熊主場",
    "Oracle Park": "巨人主場",
    "Petco Park": "教士主場",
    "T-Mobile Park": "水手主場",
    "Minute Maid Park": "太空人主場",
    "Globe Life Field": "遊騎兵主場",
    "Truist Park": "勇士主場",
    "Citi Field": "大都會主場",
    "Citizens Bank Park": "費城人主場",
    "PNC Park": "海盜主場",
    "Great American Ball Park": "紅人主場",
    "Progressive Field": "守護者主場",
    "Comerica Park": "老虎主場",
    "Kauffman Stadium": "皇家主場",
    "Target Field": "雙城主場",
    "Guaranteed Rate Field": "白襪主場",
    "American Family Field": "釀酒人主場",
    "Rogers Centre": "藍鳥主場",
    "Oriole Park": "金鶯主場",
    "Tropicana Field": "光芒主場",
    "loanDepot park": "馬林魚主場",
    "Nationals Park": "國民主場",
    "Angel Stadium": "天使主場",
    "Oakland Coliseum": "運動家主場",
    "Coors Field": "洛磯主場",
    "Chase Field": "響尾蛇主場",
}


def _parse_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def taiwan_date_from_iso(iso: str | None) -> str | None:
    dt = _parse_utc(iso)
    if not dt:
        return None
    return dt.astimezone(TPE).date().isoformat()


def taiwan_time_from_iso(iso: str | None) -> str:
    dt = _parse_utc(iso)
    if not dt:
        return ""
    return dt.astimezone(TPE).strftime("%H:%M")


def _is_us_dst(dt_utc: datetime) -> bool:
    """Approximate US DST for MLB season (Mar–Oct). Good enough without tzdata."""
    return 3 <= dt_utc.month <= 10


def _fallback_tz_offset_hours(tz_name: str, dt_utc: datetime) -> int:
    """Return UTC offset hours (e.g. -5 for US Central DST)."""
    dst = _is_us_dst(dt_utc) and tz_name != "America/Phoenix"
    std_dst = {
        "America/New_York": (-5, -4),
        "America/Chicago": (-6, -5),
        "America/Denver": (-7, -6),
        "America/Los_Angeles": (-8, -7),
        "America/Phoenix": (-7, -7),
        "America/Detroit": (-5, -4),
        "America/Toronto": (-5, -4),
    }
    pair = std_dst.get(tz_name)
    if not pair:
        return 0
    return pair[1] if dst else pair[0]


def venue_local_time_from_iso(iso: str | None, home_team_id: int | None) -> str:
    dt = _parse_utc(iso)
    if not dt or not home_team_id:
        return ""
    tz_name = _TEAM_HOME_TZ.get(int(home_team_id))
    if not tz_name:
        return ""
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")
    except Exception:
        offset_h = _fallback_tz_offset_hours(tz_name, dt)
        return (dt + timedelta(hours=offset_h)).strftime("%H:%M")


def display_venue(raw: str | None, home_team_id: int | None = None) -> str:
    name = (raw or "").strip()
    if name:
        for key, zh in _VENUE_ZH.items():
            if key in name:
                return zh
    if home_team_id:
        zh_team = team_name_zh(team_id=home_team_id)
        if zh_team:
            return f"{zh_team}主場"
    return name


def format_matchup_timing(
    game_date_iso: str | None,
    *,
    venue_raw: str | None,
    home_team_id: int,
    official_date: str | None = None,
) -> dict[str, str]:
    """NPB-style: stadium includes local first-pitch; date/timeTaiwan are Taiwan wall clock."""
    tw_date = taiwan_date_from_iso(game_date_iso) or (official_date or "")
    tw_time = taiwan_time_from_iso(game_date_iso)
    local_time = venue_local_time_from_iso(game_date_iso, home_team_id)
    venue_zh = display_venue(venue_raw, home_team_id)
    stadium = f"{venue_zh} {local_time}".strip() if venue_zh and local_time else venue_zh
    return {
        "date": tw_date,
        "timeTaiwan": tw_time,
        "timeLocal": local_time,
        "stadium": stadium,
    }


def apply_mlb_matchup_timing(data: dict[str, Any]) -> dict[str, Any]:
    """Always recompute Taiwan date/time from gameDate (cache headers are often stale)."""
    data = dict(data)
    matchup = dict(data.get("matchup") or {})
    home_id = (data.get("home") or {}).get("teamId")
    game_date = matchup.get("gameDate")
    if not home_id or not game_date:
        data["matchup"] = matchup
        return data

    timing = format_matchup_timing(
        str(game_date),
        venue_raw=str(matchup.get("venueRaw") or ""),
        home_team_id=int(home_id),
        official_date=str(matchup.get("officialDate") or matchup.get("date") or "")[:10] or None,
    )
    matchup["date"] = timing["date"]
    matchup["timeTaiwan"] = timing["timeTaiwan"]
    matchup["timeLocal"] = timing["timeLocal"]
    matchup["stadium"] = timing["stadium"]
    data["matchup"] = matchup
    return data
