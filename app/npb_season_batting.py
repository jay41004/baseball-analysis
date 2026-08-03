"""NPB full-season batting: official avg/HR/RBI + third-party RISP."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.npb_teams import TEAM_BY_CODE, TEAM_BY_ID

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
NPB_BASE = "https://npb.jp"
BASEBALL_JP_RISP = "https://www.baseball-jp.com/stats/b/sp.html"
CACHE_TTL = timedelta(hours=6)
JST = timezone(timedelta(hours=9))
_BUILD_LOCK = asyncio.Lock()
_MEMORY: dict[str, Any] | None = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NPB-Analytics/1.0)",
}

# baseball-jp short team marks -> npb.jp team codes
BJ_TEAM_TO_CODE = {
    "神": "t",
    "デ": "db",
    "巨": "g",
    "中": "d",
    "広": "c",
    "ヤ": "s",
    "ソ": "h",
    "日": "f",
    "オ": "b",
    "楽": "e",
    "西": "l",
    "ロ": "m",
}

_SPACE_RE = re.compile(r"[\s　]+")
_NAME_MARK_RE = re.compile(r"^[*＋+]+")


def _cache_path(year: int) -> Path:
    return BASE_DIR / "data" / f"npb_season_batting_{year}.json"


def _norm_name(name: str) -> str:
    text = _SPACE_RE.sub("", (name or "").strip())
    return _NAME_MARK_RE.sub("", text)


def _current_year() -> int:
    return datetime.now(JST).year


def _parse_int(text: str) -> int | None:
    text = (text or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_avg(text: str) -> str | None:
    text = (text or "").strip()
    if not text or text == "-" or text == ".---":
        return None
    if text.startswith("."):
        return text
    try:
        value = float(text)
    except ValueError:
        return None
    formatted = f"{value:.3f}"
    return formatted[1:] if formatted.startswith("0.") else formatted


def _parse_team_batting_page(html: str, *, team_code: str, team_id: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table")
    if not table:
        return []
    rows = table.select("tr")
    if len(rows) < 2:
        return []
    players: list[dict[str, Any]] = []
    for tr in rows[1:]:
        cells = [cell.get_text(" ", strip=True) for cell in tr.select("th, td")]
        # 選手 試合 打席 打数 ... 本塁打 ... 打点 ... 打率 長打率 出塁率
        if len(cells) < 21:
            continue
        name = cells[0].strip()
        if not name or name == "選手":
            continue
        avg = _parse_avg(cells[20])
        hr = _parse_int(cells[8])
        rbi = _parse_int(cells[10])
        at_bats = _parse_int(cells[3])
        hits = _parse_int(cells[5])
        players.append(
            {
                "name": name,
                "nameNorm": _norm_name(name),
                "teamCode": team_code,
                "teamId": team_id,
                "avg": avg,
                "homeRuns": hr,
                "rbi": rbi,
                "atBats": at_bats,
                "hits": hits,
            }
        )
    return players


def _parse_risp_page(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for tr in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.select("td")]
        if len(cells) < 3:
            continue
        team_mark = cells[0].strip()
        team_code = BJ_TEAM_TO_CODE.get(team_mark)
        if not team_code:
            continue
        team = TEAM_BY_CODE.get(team_code)
        if not team:
            continue
        name = cells[1].strip()
        risp = _parse_avg(cells[2])
        if not name or not risp:
            continue
        rows.append(
            {
                "name": name,
                "nameNorm": _norm_name(name),
                "teamCode": team_code,
                "teamId": team["id"],
                "rispAvg": risp,
            }
        )
    return rows


def _merge_risp(players: list[dict[str, Any]], risp_rows: list[dict[str, Any]]) -> None:
    by_team: dict[int, dict[str, dict[str, Any]]] = {}
    for row in risp_rows:
        by_team.setdefault(row["teamId"], {})[row["nameNorm"]] = row
    for player in players:
        team_map = by_team.get(player["teamId"]) or {}
        hit = team_map.get(player["nameNorm"])
        if not hit:
            # family-name / short-name fallback inside same team
            candidates = [
                row
                for norm, row in team_map.items()
                if norm.startswith(player["nameNorm"]) or player["nameNorm"].startswith(norm)
            ]
            if len(candidates) == 1:
                hit = candidates[0]
        if hit:
            player["rispAvg"] = hit["rispAvg"]


async def build_season_batting(year: int | None = None) -> dict[str, Any]:
    year = year or _current_year()
    players: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0, headers=HEADERS, follow_redirects=True) as http:
        for team in TEAM_BY_ID.values():
            code = team["code"]
            url = f"{NPB_BASE}/bis/{year}/stats/idb1_{code}.html"
            try:
                resp = await http.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.warning("NPB season batting fetch failed: %s", url)
                continue
            players.extend(
                _parse_team_batting_page(resp.text, team_code=code, team_id=team["id"])
            )
        try:
            risp_resp = await http.get(BASEBALL_JP_RISP)
            risp_resp.raise_for_status()
            _merge_risp(players, _parse_risp_page(risp_resp.text))
        except httpx.HTTPError:
            logger.warning("baseball-jp RISP fetch failed")

    payload = {
        "year": year,
        "updatedAt": datetime.now(JST).isoformat(),
        "players": players,
    }
    path = _cache_path(year)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    global _MEMORY
    _MEMORY = payload
    return payload


def _load_cache(year: int) -> dict[str, Any] | None:
    global _MEMORY
    if _MEMORY and _MEMORY.get("year") == year:
        return _MEMORY
    path = _cache_path(year)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    _MEMORY = payload
    return payload


def _cache_fresh(payload: dict[str, Any] | None) -> bool:
    if not payload or not payload.get("players"):
        return False
    updated = payload.get("updatedAt")
    if not updated:
        return False
    try:
        stamp = datetime.fromisoformat(updated)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=JST)
    return datetime.now(JST) - stamp.astimezone(JST) < CACHE_TTL


def match_season_batter(
    players: list[dict[str, Any]], *, team_id: int, lineup_name: str
) -> dict[str, Any] | None:
    needle = _norm_name(lineup_name)
    if not needle:
        return None
    team_players = [p for p in players if p.get("teamId") == team_id]
    exact = [p for p in team_players if p.get("nameNorm") == needle]
    if len(exact) == 1:
        return exact[0]
    prefixed = [
        p
        for p in team_players
        if (p.get("nameNorm") or "").startswith(needle)
    ]
    if len(prefixed) == 1:
        return prefixed[0]
    # Lineup sometimes uses full name while season page uses short given-name-only
    contained = [
        p
        for p in team_players
        if needle.startswith(p.get("nameNorm") or "") and p.get("nameNorm")
    ]
    if len(contained) == 1:
        return contained[0]
    return None


async def get_season_batting_lookup(
    year: int | None = None, *, force: bool = False
) -> dict[str, Any]:
    year = year or _current_year()
    cached = _load_cache(year)
    if not force and _cache_fresh(cached):
        return cached or {"year": year, "players": []}
    async with _BUILD_LOCK:
        cached = _load_cache(year)
        if not force and _cache_fresh(cached):
            return cached or {"year": year, "players": []}
        try:
            return await build_season_batting(year)
        except Exception:
            logger.exception("NPB season batting build failed")
            return cached or {"year": year, "players": []}


def season_fields_for_batter(
    lookup: dict[str, Any], *, team_id: int, lineup_name: str
) -> dict[str, Any]:
    player = match_season_batter(
        lookup.get("players") or [], team_id=team_id, lineup_name=lineup_name
    )
    if not player:
        return {}
    fields: dict[str, Any] = {}
    if player.get("avg") is not None:
        fields["avg"] = player["avg"]
    if player.get("homeRuns") is not None:
        fields["homeRuns"] = player["homeRuns"]
    if player.get("rbi") is not None:
        fields["rbi"] = player["rbi"]
    if player.get("rispAvg") is not None:
        fields["rispAvg"] = player["rispAvg"]
    return fields
