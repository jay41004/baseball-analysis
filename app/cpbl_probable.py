"""Fallback probable pitchers from PTT Baseball daily posts."""

from __future__ import annotations

import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.cpbl_teams import TEAMS, match_team

PTT_SEARCH = "https://www.ptt.cc/bbs/Baseball/search?q=%E5%85%88%E7%99%BC%E6%8A%95%E6%89%8B"
PTT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Cookie": "over18=1",
}

_ARTICLE_LINK_RE = re.compile(
    r'<a href="(/bbs/Baseball/M\.[^"]+)">(\[[^\]]+\]\s*)?CPBL\s+\d+/\d+\s+先發投手\s*</a>'
)


def _extract_article_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#main-content")
    if not main:
        return ""
    text = main.get_text("\n", strip=True)
    for marker in ("--", "※ 發信站"):
        pos = text.find(marker)
        if pos >= 0:
            text = text[:pos]
    return text


def _line_is_team(line: str) -> dict[str, Any] | None:
    normalized = line.replace(" ", "").replace("　", "")
    for team in TEAMS:
        for alias in team["aliases"]:
            alias_norm = alias.replace(" ", "").replace("　", "")
            if normalized == alias_norm:
                return team
    return None


def _parse_ptt_block(block: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if not lines:
        return None

    sno_match = re.match(r"(\d+)", lines[0])
    if not sno_match:
        return None
    game_sno = int(sno_match.group(1))

    pairs: list[tuple[dict[str, Any], str]] = []
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if (
            line.startswith("2026")
            or "2026 VS" in line
            or "ERA" in line
            or "預報" in line
            or line.startswith("※")
        ):
            idx += 1
            continue
        team = _line_is_team(line)
        if team and idx + 1 < len(lines):
            pitcher = lines[idx + 1]
            if (
                not pitcher.startswith("2026")
                and _line_is_team(pitcher) is None
                and not re.match(r"\d+/\d+", pitcher)
            ):
                pairs.append((team, pitcher))
                idx += 2
                continue
        idx += 1

    if len(pairs) < 2:
        return None

    return {
        "gameSno": game_sno,
        "awayTeamId": pairs[0][0]["id"],
        "homeTeamId": pairs[1][0]["id"],
        "awayProbablePitcher": pairs[0][1],
        "homeProbablePitcher": pairs[1][1],
    }


def _normalize_pitcher_post(text: str) -> list[dict[str, Any]]:
    games: list[dict[str, Any]] = []
    for block in re.split(r"場地:\s*#", text):
        parsed = _parse_ptt_block(block)
        if parsed:
            games.append(parsed)
    return games


async def fetch_ptt_probable_pitchers(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    try:
        index = await http.get(PTT_SEARCH, headers=PTT_HEADERS)
        index.raise_for_status()
    except httpx.HTTPError:
        return []

    link_match = _ARTICLE_LINK_RE.search(index.text)
    article_path = link_match.group(1) if link_match else None
    if not article_path:
        for match in re.finditer(
            r'<a href="(/bbs/Baseball/M\.[^"]+)">([^<]+)</a>', index.text
        ):
            title = match.group(2)
            if "CPBL" in title and "先發投手" in title:
                article_path = match.group(1)
                break
    if not article_path:
        return []

    try:
        article = await http.get(
            f"https://www.ptt.cc{article_path}",
            headers=PTT_HEADERS,
        )
        article.raise_for_status()
    except httpx.HTTPError:
        return []

    return _normalize_pitcher_post(_extract_article_text(article.text))


async def enrich_schedule_from_ptt(http: httpx.AsyncClient, games: list[dict[str, Any]]) -> None:
    ptt_games = await fetch_ptt_probable_pitchers(http)
    if not ptt_games:
        return

    by_sno = {game["gameSno"]: game for game in ptt_games}

    for game in games:
        if game.get("awayProbablePitcher") and game.get("homeProbablePitcher"):
            continue
        source = by_sno.get(game.get("gameSno"))
        if not source:
            continue
        if not game.get("awayProbablePitcher"):
            game["awayProbablePitcher"] = source.get("awayProbablePitcher")
        if not game.get("homeProbablePitcher"):
            game["homeProbablePitcher"] = source.get("homeProbablePitcher")
