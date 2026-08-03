"""NPB batter vs pitcher AVG via baseball-pitcher-vs-batter.com API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.npb_season_batting import _norm_name
from app.npb_teams import TEAM_BY_ID

logger = logging.getLogger(__name__)

BPVB_API = "https://baseball-pitcher-vs-batter.com/baseball/api"
JST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NPB-Analytics/1.0)",
    "Accept": "application/json",
}

# Our team id -> BPVB teamId (by franchise).
OUR_TO_BPVB_TEAM: dict[int, int] = {
    1: 2,  # 巨人
    2: 4,  # 阪神
    3: 6,  # 中日
    4: 5,  # 広島
    5: 1,  # ヤクルト
    6: 3,  # DeNA
    7: 7,  # ソフトバンク
    8: 8,  # 日本ハム
    9: 10,  # オリックス
    10: 12,  # 楽天
    11: 9,  # 西武
    12: 11,  # ロッテ
}

_pitcher_list_cache: dict[tuple[int, str], list[dict[str, Any]]] = {}


def _current_year() -> int:
    return datetime.now(JST).year


def _format_avg(value: Any) -> str | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    text = f"{number:.3f}"
    return text[1:] if text.startswith("0.") else text


def _name_matches(query: str, candidate: str) -> bool:
    left = _norm_name(query)
    right = _norm_name(candidate)
    if not left or not right:
        return False
    return left == right or left in right or right in left


async def _get_json(
    http: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    try:
        resp = await http.get(f"{BPVB_API}{path}", params=params)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type") or ""
        if "application/json" not in content_type and resp.text.lstrip().startswith("<"):
            # Site sometimes returns the SPA shell when a year has no matchup rows.
            return {"message": "Empty", "data": {"matchResult": []}}
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("BPVB request failed %s: %s", path, exc)
        return None


async def _pitcher_list(
    http: httpx.AsyncClient, *, bpvb_team_id: int, year: str
) -> list[dict[str, Any]]:
    key = (bpvb_team_id, year)
    cached = _pitcher_list_cache.get(key)
    if cached is not None:
        return cached
    payload = await _get_json(
        http, "/getPitcherList", {"teamId": bpvb_team_id, "year": year}
    )
    rows = ((payload or {}).get("data") or {}).get("pitcherList") or []
    _pitcher_list_cache[key] = rows
    return rows


async def resolve_pitcher_id(
    http: httpx.AsyncClient,
    *,
    our_team_id: int,
    pitcher_name: str,
    year: int | None = None,
) -> int | None:
    bpvb_team = OUR_TO_BPVB_TEAM.get(our_team_id)
    if not bpvb_team or not pitcher_name:
        return None
    year_text = str(year or _current_year())
    for list_year in (year_text, "通算"):
        rows = await _pitcher_list(http, bpvb_team_id=bpvb_team, year=list_year)
        matches = [
            row for row in rows if _name_matches(pitcher_name, row.get("playerNm") or "")
        ]
        if len(matches) == 1:
            return int(matches[0]["playerId"])
        if len(matches) > 1:
            matches.sort(
                key=lambda row: len(_norm_name(row.get("playerNm") or "")), reverse=True
            )
            return int(matches[0]["playerId"])
    return None


async def _match_rows(
    http: httpx.AsyncClient,
    *,
    pitcher_team_id: int,
    batter_team_id: int,
    pitcher_id: int,
    year: str,
) -> list[dict[str, Any]]:
    pitcher_bpvb = OUR_TO_BPVB_TEAM.get(pitcher_team_id)
    batter_bpvb = OUR_TO_BPVB_TEAM.get(batter_team_id)
    if not pitcher_bpvb or not batter_bpvb:
        return []
    payload = await _get_json(
        http,
        "/matchResultSearch",
        {
            "pitcherTeamId": pitcher_bpvb,
            "batterTeamId": batter_bpvb,
            "pitcherId": pitcher_id,
            "selectedYear": year,
        },
    )
    return ((payload or {}).get("data") or {}).get("matchResult") or []


def _lookup_avg(rows: list[dict[str, Any]], lineup_name: str) -> str | None:
    needle = _norm_name(lineup_name)
    if not needle:
        return None
    exact: list[dict[str, Any]] = []
    fuzzy: list[dict[str, Any]] = []
    for row in rows:
        name = (row.get("batterNm") or "").strip()
        if not name:
            continue
        norm = _norm_name(name)
        entry = {"avg": _format_avg(row.get("battingAverage")), "nameNorm": norm}
        if norm == needle:
            exact.append(entry)
        elif needle in norm or norm in needle:
            fuzzy.append(entry)
    if len(exact) == 1:
        return exact[0]["avg"]
    if len(exact) > 1:
        return exact[0]["avg"]
    if len(fuzzy) == 1:
        return fuzzy[0]["avg"]
    # Prefer the longest full name when several fuzzy hits share a family name.
    if len(fuzzy) > 1:
        fuzzy.sort(key=lambda item: len(item["nameNorm"]), reverse=True)
        # Only accept if exactly one starts with / ends with the short lineup name.
        prefixed = [
            item
            for item in fuzzy
            if item["nameNorm"].startswith(needle) or item["nameNorm"].endswith(needle)
        ]
        if len(prefixed) == 1:
            return prefixed[0]["avg"]
        if len(prefixed) > 1:
            return prefixed[0]["avg"]
    return None


async def enrich_batters_vs_pitcher(
    batters: list[dict[str, Any]],
    *,
    batter_team_id: int,
    pitcher_team_id: int,
    pitcher_name: str | None,
    year: int | None = None,
) -> list[dict[str, Any]]:
    """Attach vsPitcherSeasonAvg / vsPitcherCareerAvg for opposing starter."""
    if not batters or not pitcher_name:
        return batters
    if batter_team_id not in TEAM_BY_ID or pitcher_team_id not in TEAM_BY_ID:
        return batters

    year = year or _current_year()
    async with httpx.AsyncClient(
        timeout=30.0, headers=HEADERS, follow_redirects=True
    ) as http:
        pitcher_id = await resolve_pitcher_id(
            http, our_team_id=pitcher_team_id, pitcher_name=pitcher_name, year=year
        )
        if not pitcher_id:
            logger.info("NPB vs-pitcher: unresolved pitcher %s", pitcher_name)
            return batters
        season_rows, career_rows = await asyncio.gather(
            _match_rows(
                http,
                pitcher_team_id=pitcher_team_id,
                batter_team_id=batter_team_id,
                pitcher_id=pitcher_id,
                year=str(year),
            ),
            _match_rows(
                http,
                pitcher_team_id=pitcher_team_id,
                batter_team_id=batter_team_id,
                pitcher_id=pitcher_id,
                year="通算",
            ),
        )

    enriched: list[dict[str, Any]] = []
    for batter in batters:
        copy = dict(batter)
        name = copy.get("name") or ""
        season_avg = _lookup_avg(season_rows, name)
        career_avg = _lookup_avg(career_rows, name)
        if season_avg is not None:
            copy["vsPitcherSeasonAvg"] = season_avg
        if career_avg is not None:
            copy["vsPitcherCareerAvg"] = career_avg
        enriched.append(copy)
    return enriched
