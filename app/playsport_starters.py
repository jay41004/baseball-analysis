"""
playsport.cc / starters_override.json starter pitcher source.

Strategy:
1. Try to read data/starters_override.json for today's date (manual/browser-collected data)
2. Try scraping playsport.cc using gameid pattern (YYYYMMDD61001/2/3 for CPBL)
   - playsport blocks Python requests with Cloudflare 403 usually
   - Kept as best-effort: works if Cloudflare is relaxed
3. Return whatever was found

Override JSON format (data/starters_override.json):
{
  "YYYY-MM-DD": {
    "cpbl": [
      {"awayTeam": "味全龍", "homeTeam": "富邦悍將", "awayStarter": "曾仁和", "homeStarter": "鈴木駿輔"},
      ...
    ],
    "mlb": [...],
    "npb": [...]
  }
}
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://www.playsport.cc"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": BASE,
}

OVERRIDE_FILE = Path(__file__).resolve().parent.parent / "data" / "starters_override.json"

# playsport league alliance IDs
_ALLIANCE = {"cpbl": 6, "npb": 2, "mlb": 1}

# CPBL gameid suffix pattern: YYYYMMDD6100{1,2,3,...} for games 1,2,3
_CPBL_GAME_SLOTS = [1, 2, 3]


def _today_tw() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def _load_override(target_date: date) -> list[dict[str, Any]]:
    """Load starters from override JSON if available for target_date."""
    if not OVERRIDE_FILE.exists():
        return []
    try:
        raw = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    date_str = target_date.isoformat()
    day_data = raw.get(date_str, {})
    results = []
    for league, games in day_data.items():
        for g in games:
            results.append({
                "source": "override",
                "league": league,
                "gameDate": date_str,
                "awayTeam": g.get("awayTeam", ""),
                "homeTeam": g.get("homeTeam", ""),
                "awayStarter": g.get("awayStarter"),
                "homeStarter": g.get("homeStarter"),
                "gameTime": g.get("gameTime"),
            })
    return results


async def _fetch_game_page(
    http: httpx.AsyncClient, url: str
) -> dict[str, Any]:
    """Fetch a playsport battle page and extract starters + team names."""
    try:
        resp = await http.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return {}
    except httpx.HTTPError as exc:
        logger.debug("playsport fetch failed (%s): %s", url, exc)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title: "中華職棒 145 味全VS富邦 對戰資訊" or "中華職棒 台鋼VS統一 對戰資訊"
    title = soup.title.get_text(strip=True) if soup.title else ""
    away_team = home_team = ""
    m = re.search(r"(\S+)VS(\S+)\s+對戰資訊", title)
    if m:
        away_team = m.group(1)
        home_team = m.group(2)

    # Find 先發投手 section → look for pitcher name headings after PM/AM time heading
    away_starter = home_starter = game_time = None
    for h in soup.find_all(["h1", "h2", "h3"]):
        if "先發投手" in h.get_text():
            parent = h.find_parent()
            if parent:
                h4 = parent.find("h4")
                if h4:
                    game_time = h4.get_text(strip=True)
                    # Get next two sibling headings as pitcher names
                    pitchers = []
                    node = h4.find_next_sibling()
                    while node and len(pitchers) < 2:
                        txt = node.get_text(strip=True)
                        if txt and txt not in (away_team, home_team) and txt != game_time:
                            if re.match(r"[A-Za-zÀ-ÿ\u4e00-\u9fff·\s·]{2,30}$", txt):
                                pitchers.append(txt)
                        node = node.find_next_sibling()
                    if len(pitchers) >= 2:
                        away_starter, home_starter = pitchers[0], pitchers[1]
                    elif len(pitchers) == 1:
                        away_starter = pitchers[0]
            break

    return {
        "awayTeam": away_team,
        "homeTeam": home_team,
        "awayStarter": away_starter,
        "homeStarter": home_starter,
        "gameTime": game_time,
    }


async def _fetch_cpbl_games(
    http: httpx.AsyncClient, target_date: date
) -> list[dict[str, Any]]:
    """Try to scrape CPBL starters using gameid pattern."""
    date_compact = target_date.strftime("%Y%m%d")
    results = []
    for slot in _CPBL_GAME_SLOTS:
        gameid = f"{date_compact}6100{slot}"
        url = f"{BASE}/gamesData/battle?allianceid={_ALLIANCE['cpbl']}&gameid={gameid}"
        detail = await _fetch_game_page(http, url)
        if detail.get("awayTeam") or detail.get("awayStarter"):
            results.append({
                "source": "playsport",
                "league": "cpbl",
                **{k: detail.get(k) for k in ("awayTeam", "homeTeam", "awayStarter", "homeStarter", "gameTime")},
            })
    return results


async def _fetch_mlb_today_game(
    http: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Fetch the default playsport MLB page (shows today's first game)."""
    detail = await _fetch_game_page(http, f"{BASE}/gamesData/battle?allianceid={_ALLIANCE['mlb']}")
    if detail.get("awayStarter"):
        return [{
            "source": "playsport",
            "league": "mlb",
            **{k: detail.get(k) for k in ("awayTeam", "homeTeam", "awayStarter", "homeStarter", "gameTime")},
        }]
    return []


async def fetch_playsport_starters(
    http: httpx.AsyncClient,
    target_date: date | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch today's starters for CPBL/MLB/NPB.
    Priority: override JSON → playsport scrape.
    """
    if target_date is None:
        target_date = _today_tw()

    # 1. Override JSON (manual/browser-collected data)
    results = _load_override(target_date)
    if results:
        logger.info("playsport: loaded %d games from override JSON for %s", len(results), target_date)
        return results

    # 2. Try scraping playsport (may be blocked by Cloudflare)
    cpbl = await _fetch_cpbl_games(http, target_date)
    results.extend(cpbl)

    # MLB default page
    mlb = await _fetch_mlb_today_game(http)
    results.extend(mlb)

    if results:
        logger.info("playsport: scraped %d games for %s", len(results), target_date)
    else:
        logger.info("playsport: no data available for %s (Cloudflare may be blocking)", target_date)

    return results


def save_starters_override(
    starters: list[dict[str, Any]],
    target_date: date | None = None,
) -> None:
    """
    Utility: save a list of starter dicts to the override JSON.
    Each dict should have: league, awayTeam, homeTeam, awayStarter, homeStarter.
    """
    if target_date is None:
        target_date = _today_tw()
    date_str = target_date.isoformat()

    existing: dict[str, Any] = {}
    if OVERRIDE_FILE.exists():
        try:
            existing = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    by_league: dict[str, list[dict[str, Any]]] = {}
    for s in starters:
        league = s.get("league", "cpbl")
        by_league.setdefault(league, []).append({
            "awayTeam": s.get("awayTeam", ""),
            "homeTeam": s.get("homeTeam", ""),
            "awayStarter": s.get("awayStarter"),
            "homeStarter": s.get("homeStarter"),
            "gameTime": s.get("gameTime"),
        })

    existing[date_str] = by_league
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Saved starters override for %s (%d games)", date_str, len(starters))
