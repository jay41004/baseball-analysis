"""Fallback CPBL data from stats.cpbl.com.tw (HTML / RSC payloads)."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import Any

import httpx

from app.cpbl_teams import match_team

STATS_BASE = "https://stats.cpbl.com.tw"
STATS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

_SPORTS_EVENT_RE = re.compile(
    r'\{"@type":"SportsEvent","name":"(?P<name>[^"]*)","description":"(?P<desc>[^"]*)"'
    r'.*?"startDate":"(?P<start>[^"]*)","eventStatus":"(?P<status>[^"]*)"'
    r'.*?"url":"[^"]*2026-A-(?P<sno>\d+)"'
    r'.*?"location":\{"@type":"Place","name":"(?P<stadium>[^"]*)"\}'
    r'.*?"homeTeam":\{"@type":"SportsTeam","name":"(?P<home>[^"]*)"'
    r'.*?"awayTeam":\{"@type":"SportsTeam","name":"(?P<away>[^"]*)"',
    re.S,
)
_INNING_SCORE_RE = re.compile(r'\\"inningScore\\":\[(.*?)\]')
_TITLE_SCORE_RE = re.compile(
    r"<title>(?P<away>[^<]+?)\s+(?P<away_score>\d+):(?P<home_score>\d+)\s+(?P<home>[^|]+)"
)
_TITLE_VS_RE = re.compile(r"<title>(?P<away>[^<]+?) vs (?P<home>[^|]+)")
_META_DATE_RE = re.compile(r"content=\"一軍例行賽 \| (\d{4}/\d{1,2}/\d{1,2})")


def _starter_from_pitchers_inner(inner: str) -> str | None:
    if not inner.strip():
        return None
    block = "[" + inner.replace('\\"', '"') + "]"
    try:
        rows = json.loads(block)
    except json.JSONDecodeError:
        return None
    for row in rows:
        if (row.get("roleType") or "").strip() == "先發":
            name = (row.get("pitcherName") or "").strip()
            if name:
                return name
    return None


def _stats_game_chunk(html: str, *, game_sno: int, year: int, size: int = 250_000) -> str:
    marker = f'\\"gameId\\":\\"{year}-A-{game_sno}\\"'
    idx = html.find(marker)
    if idx < 0:
        return ""
    return html[idx : idx + size]


def parse_stats_pitching(html: str, *, game_sno: int, year: int) -> list[dict[str, Any]]:
    """Extract away/home pitching lines from stats.cpbl RSC payloads."""
    chunk = _stats_game_chunk(html, game_sno=game_sno, year=year)
    if not chunk:
        return []

    pitching: list[dict[str, Any]] = []
    arrays = [
        match.group(1)
        for match in re.finditer(r'\\"pitchers\\":\[(.*?)\]', chunk)
        if match.group(1).strip()
    ]
    for side_type, block in zip((1, 2), arrays[:2]):
        try:
            rows = json.loads("[" + block.replace('\\"', '"') + "]")
        except json.JSONDecodeError:
            continue
        for row in rows:
            pitching.append(
                {
                    "VisitingHomeType": side_type,
                    "PitcherName": (row.get("pitcherName") or "").strip(),
                    "RoleType": (row.get("roleType") or "").strip(),
                    "InningPitchedCnt": row.get("inningPitchedCnt"),
                    "RunCnt": row.get("runCnt"),
                    "EarnedRunCnt": row.get("earnedRunCnt"),
                }
            )
    return pitching


def parse_stats_starters(html: str, *, game_sno: int, year: int) -> tuple[str | None, str | None]:
    """Extract away/home starters from stats.cpbl RSC payloads."""
    chunk = _stats_game_chunk(html, game_sno=game_sno, year=year, size=120_000)
    if not chunk:
        return None, None
    arrays = [
        match.group(1)
        for match in re.finditer(r'\\"pitchers\\":\[(.*?)\]', chunk)
        if match.group(1).strip()
    ]
    away = _starter_from_pitchers_inner(arrays[0]) if arrays else None
    home = _starter_from_pitchers_inner(arrays[1]) if len(arrays) > 1 else None
    return away, home


def _decode_escaped_json_array(block: str) -> list[dict[str, Any]]:
    text = "[" + block.replace('\\"', '"') + "]"
    return json.loads(text)


def parse_stats_pbp_plays(html: str, *, game_sno: int, year: int) -> list[dict[str, Any]]:
    chunk = _stats_game_chunk(html, game_sno=game_sno, year=year, size=800_000)
    if not chunk:
        chunk = html

    # Nested fields (e.g. trackman objects) break flat [^}]* regex; use brace matching.
    normalized = chunk.replace('\\"', '"')
    objects: list[dict[str, Any]] = []
    needle = '"hitterAcnt":"'
    start = 0
    while True:
        idx = normalized.find(needle, start)
        if idx < 0:
            break
        brace = normalized.rfind("{", 0, idx)
        if brace < 0:
            start = idx + len(needle)
            continue
        depth = 0
        end: int | None = None
        for i in range(brace, len(normalized)):
            ch = normalized[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            start = idx + len(needle)
            continue
        try:
            obj = json.loads(normalized[brace:end])
        except json.JSONDecodeError:
            start = idx + len(needle)
            continue
        if obj.get("hitterAcnt") and (obj.get("year") or obj.get("gameSno") is not None):
            objects.append(obj)
        start = end
    return objects


def parse_stats_lineup(html: str, *, game_sno: int, year: int) -> list[dict[str, Any]]:
    """Build FirstSno-style starting lineup rows from stats.cpbl play-by-play."""
    plays = parse_stats_pbp_plays(html, game_sno=game_sno, year=year)
    ordered = sorted(
        plays,
        key=lambda play: (
            int(play.get("inningSeq") or 0),
            play.get("mainEventNo") or "",
            int(play.get("pitchCnt") or 0),
        ),
    )

    first_sno: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for play in ordered:
        side = int(play.get("visitingHomeType") or 0)
        slot = int(play.get("hitterLineup") or 0)
        if side not in (1, 2) or slot < 1 or slot > 9:
            continue
        key = (side, slot)
        if key in seen:
            continue
        seen.add(key)
        first_sno.append(
            {
                "VisitingHomeType": side,
                "Lineup": slot,
                "Acnt": play.get("hitterAcnt"),
                "CHName": (play.get("hitterName") or "").strip(),
                "DefendStationCode": play.get("defendStationCode")
                or play.get("hitterDefendStation")
                or "",
            }
        )
    return first_sno


def _game_hit_count(row: dict[str, Any]) -> int:
    return sum(
        int(row.get(key) or 0)
        for key in ("oneBaseHitCnt", "twoBaseHitCnt", "threeBaseHitCnt", "homeRunCnt")
    )


def _game_at_bats(row: dict[str, Any]) -> int:
    plate_appearances = int(row.get("plateAppearances") or 0)
    if plate_appearances <= 0:
        return 0
    non_ab = sum(
        int(row.get(key) or 0)
        for key in (
            "basesOnBallsCnt",
            "hitByPitchCnt",
            "sacrificeFlyCnt",
            "sacrificeHitCnt",
            "intentionalBasesOnBallsCnt",
        )
    )
    return max(plate_appearances - non_ab, 0)


def parse_stats_batting(html: str, *, game_sno: int, year: int) -> list[dict[str, Any]]:
    """Extract per-game batting lines from stats.cpbl RSC hitter summaries."""
    chunk = _stats_game_chunk(html, game_sno=game_sno, year=year, size=700_000)
    if not chunk:
        return []

    batting: list[dict[str, Any]] = []
    arrays = re.findall(r'\\"hitters\\":\[(.*?)\]', chunk)
    for side_type, block in zip((1, 2), arrays[:2]):
        if not block.strip():
            continue
        try:
            rows = _decode_escaped_json_array(block)
        except json.JSONDecodeError:
            continue
        for row in rows:
            station = str(row.get("defendStation") or "")
            batting.append(
                {
                    "VisitingHomeType": side_type,
                    "HitterAcnt": row.get("hitterAcnt"),
                    "Acnt": row.get("hitterAcnt"),
                    "CHName": (row.get("hitterName") or "").strip(),
                    "HitCnt": _game_hit_count(row),
                    "PlateAppearances": _game_at_bats(row),
                    "OneBaseHitCnt": int(row.get("oneBaseHitCnt") or 0),
                    "TwoBaseHitCnt": int(row.get("twoBaseHitCnt") or 0),
                    "ThreeBaseHitCnt": int(row.get("threeBaseHitCnt") or 0),
                    "BasesOnBallsCnt": int(row.get("basesOnBallsCnt") or 0),
                    "HitByPitchCnt": int(row.get("hitByPitchCnt") or 0),
                    "SacrificeFlyCnt": int(row.get("sacrificeFlyCnt") or 0),
                    "RoleType": "代打" if "(PH)" in station else "先發",
                    "HomeRunCnt": int(row.get("homeRunCnt") or 0),
                    "RunBattedInCnt": int(row.get("runBattedInCnt") or 0),
                    "SeasonAvg": row.get("avg"),
                }
            )
    return batting


def attach_stats_lineup_data(
    payload: dict[str, Any], html: str, *, game_sno: int, year: int
) -> None:
    if not payload.get("firstSno"):
        payload["firstSno"] = parse_stats_lineup(html, game_sno=game_sno, year=year)
    if not payload.get("batting"):
        payload["batting"] = parse_stats_batting(html, game_sno=game_sno, year=year)


def _apply_stats_starters(game: dict[str, Any], html: str) -> None:
    game_sno = game.get("gameSno")
    year = int(game.get("year") or date.today().year)
    if game_sno is None:
        return
    away, home = parse_stats_starters(html, game_sno=int(game_sno), year=year)
    if away and not game.get("awayProbablePitcher"):
        game["awayProbablePitcher"] = away
    if home and not game.get("homeProbablePitcher"):
        game["homeProbablePitcher"] = home


def _parse_inning_blocks(html: str) -> list[list[int]]:
    blocks: list[list[int]] = []
    for block in _INNING_SCORE_RE.findall(html):
        rows = json.loads("[" + block.replace('\\"', '"') + "]")
        innings = [0] * 9
        for row in rows:
            seq = int(row.get("seq") or 0)
            if 1 <= seq <= 9:
                innings[seq - 1] = int(row.get("score") or 0)
        blocks.append(innings)
    return blocks


def _normalize_stats_event(match: re.Match[str]) -> dict[str, Any] | None:
    away_team = match_team(match.group("away"))
    home_team = match_team(match.group("home"))
    if not away_team or not home_team:
        return None

    iso_date = (match.group("start") or "")[:10]
    year = int(iso_date[:4]) if iso_date else date.today().year
    game_sno = int(match.group("sno"))
    if iso_date and iso_date > date.today().isoformat():
        status = "Scheduled"
    else:
        status = "Final"

    return {
        "gameSno": game_sno,
        "year": year,
        "date": iso_date,
        "awayTeamId": away_team["id"],
        "homeTeamId": home_team["id"],
        "awayNameZh": away_team["nameZh"],
        "homeNameZh": home_team["nameZh"],
        "awayScore": None,
        "homeScore": None,
        "status": status,
        "stadium": match.group("stadium") or "",
        "awayProbablePitcher": None,
        "homeProbablePitcher": None,
    }


def _parse_stats_game_page(html: str, *, game_sno: int, year: int) -> dict[str, Any] | None:
    score_match = _TITLE_SCORE_RE.search(html)
    vs_match = _TITLE_VS_RE.search(html) if not score_match else None
    if not score_match and not vs_match:
        return None

    if score_match:
        away_team = match_team(score_match.group("away"))
        home_team = match_team(score_match.group("home"))
        away_score = int(score_match.group("away_score"))
        home_score = int(score_match.group("home_score"))
        status = "Final"
    else:
        assert vs_match is not None
        away_team = match_team(vs_match.group("away"))
        home_team = match_team(vs_match.group("home"))
        away_score = home_score = None
        status = "Scheduled"

    if not away_team or not home_team:
        return None

    iso_date = ""
    meta_date = _META_DATE_RE.search(html)
    if meta_date:
        parts = meta_date.group(1).split("/")
        if len(parts) == 3:
            iso_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"

    game = {
        "gameSno": game_sno,
        "year": year,
        "date": iso_date,
        "awayTeamId": away_team["id"],
        "homeTeamId": home_team["id"],
        "awayNameZh": away_team["nameZh"],
        "homeNameZh": home_team["nameZh"],
        "awayScore": away_score,
        "homeScore": home_score,
        "status": status,
        "stadium": "",
        "awayProbablePitcher": None,
        "homeProbablePitcher": None,
    }
    _apply_stats_starters(game, html)
    return game


async def _fetch_stats_game_html(
    http: httpx.AsyncClient, game_sno: int | str, year: int, *, retries: int = 3
) -> str | None:
    url = f"{STATS_BASE}/schedule/{year}-A-{game_sno}"
    for attempt in range(retries):
        try:
            response = await http.get(url, headers=STATS_HEADERS, timeout=30.0)
        except httpx.HTTPError:
            if attempt + 1 >= retries:
                return None
            await asyncio.sleep(0.4 * (attempt + 1))
            continue
        if response.status_code == 200:
            return response.text
        if response.status_code in {429, 503} and attempt + 1 < retries:
            await asyncio.sleep(0.8 * (attempt + 1))
            continue
        return None
    return None


async def _enrich_game_from_page(http: httpx.AsyncClient, game: dict[str, Any]) -> dict[str, Any]:
    game_sno = game.get("gameSno")
    year = int(game.get("year") or date.today().year)
    if game_sno is None:
        return game
    html = await _fetch_stats_game_html(http, game_sno, year)
    if not html:
        return game

    if not game.get("awayTeamId") or not game.get("homeTeamId"):
        parsed = _parse_stats_game_page(html, game_sno=int(game_sno), year=year)
        if parsed:
            game = parsed
    else:
        _apply_stats_starters(game, html)
    return game


async def _discover_max_game_sno(http: httpx.AsyncClient, year: int) -> int:
    lo, hi = 1, 500
    while lo < hi:
        mid = (lo + hi + 1) // 2
        html = await _fetch_stats_game_html(http, mid, year)
        if html and (
            "中華職棒" in html
            or _TITLE_VS_RE.search(html)
            or _TITLE_SCORE_RE.search(html)
        ):
            lo = mid
        else:
            hi = mid - 1
    return lo


async def _expand_stats_schedule_range(
    http: httpx.AsyncClient,
    games: list[dict[str, Any]],
    seen: set[int],
    year: int,
) -> list[dict[str, Any]]:
    """Fill schedule holes: stats index skips many completed game snos."""
    discovered_max = await _discover_max_game_sno(http, year)
    if not seen:
        missing = list(range(1, discovered_max + 1))
    else:
        missing = [sno for sno in range(1, discovered_max + 1) if sno not in seen]
    if not missing:
        return games

    sem = asyncio.Semaphore(10)

    async def load(game_sno: int) -> dict[str, Any] | None:
        async with sem:
            html = await _fetch_stats_game_html(http, game_sno, year)
            if not html:
                return None
            return _parse_stats_game_page(html, game_sno=game_sno, year=year)

    loaded = await asyncio.gather(*[load(game_sno) for game_sno in missing])
    for game in loaded:
        if not game:
            continue
        game_sno = game.get("gameSno")
        if game_sno is None or game_sno in seen:
            continue
        seen.add(int(game_sno))
        games.append(game)

    still_missing = [sno for sno in range(1, discovered_max + 1) if sno not in seen]
    if still_missing:
        retry_loaded = await asyncio.gather(*[load(game_sno) for game_sno in still_missing])
        for game in retry_loaded:
            if not game:
                continue
            game_sno = game.get("gameSno")
            if game_sno is None or game_sno in seen:
                continue
            seen.add(int(game_sno))
            games.append(game)
    return games


async def fetch_stats_schedule(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    year = date.today().year
    response = await http.get(f"{STATS_BASE}/schedule", headers=STATS_HEADERS)
    response.raise_for_status()
    games: list[dict[str, Any]] = []
    seen: set[int] = set()

    for match in _SPORTS_EVENT_RE.finditer(response.text):
        normalized = _normalize_stats_event(match)
        if not normalized:
            continue
        game_sno = normalized["gameSno"]
        if game_sno in seen:
            continue
        seen.add(game_sno)
        games.append(normalized)

    for game_sno in sorted(
        int(sno) for sno in re.findall(rf"/schedule/{year}-A-(\d+)", response.text)
    ):
        if game_sno in seen:
            continue
        seen.add(game_sno)
        games.append(
            {
                "gameSno": game_sno,
                "year": year,
                "date": "",
                "awayTeamId": None,
                "homeTeamId": None,
                "awayNameZh": "",
                "homeNameZh": "",
                "awayScore": None,
                "homeScore": None,
                "status": "Scheduled",
                "stadium": "",
                "awayProbablePitcher": None,
                "homeProbablePitcher": None,
            }
        )

    to_enrich = [game for game in games if not game.get("awayTeamId")]
    if to_enrich:
        sem = asyncio.Semaphore(6)

        async def enrich(game: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await _enrich_game_from_page(http, game)

        enriched = await asyncio.gather(*[enrich(game) for game in to_enrich])
        by_sno = {game["gameSno"]: game for game in games if game.get("gameSno") is not None}
        for game in enriched:
            if game.get("gameSno") is not None:
                by_sno[game["gameSno"]] = game
        games = list(by_sno.values())
        seen = {int(game["gameSno"]) for game in games if game.get("gameSno") is not None}

    games = await _expand_stats_schedule_range(http, games, seen, year)

    needs_starters = [
        game
        for game in games
        if game.get("gameSno") is not None
        and (not game.get("awayProbablePitcher") or not game.get("homeProbablePitcher"))
    ]
    if needs_starters:
        sem = asyncio.Semaphore(6)

        async def enrich_starters(game: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await _enrich_game_from_page(http, game)

        updated = await asyncio.gather(*[enrich_starters(game) for game in needs_starters])
        by_sno = {game["gameSno"]: game for game in games if game.get("gameSno") is not None}
        for game in updated:
            if game.get("gameSno") is not None:
                by_sno[game["gameSno"]] = game
        games = list(by_sno.values())

    try:
        from app.cpbl_official import enrich_schedule_probable_pitchers

        await enrich_schedule_probable_pitchers(http, games)
    except httpx.HTTPError:
        pass

    try:
        from app.cpbl_probable import enrich_schedule_from_ptt

        await enrich_schedule_from_ptt(http, games)
    except httpx.HTTPError:
        pass

    games.sort(key=lambda game: (game.get("date", ""), game.get("gameSno") or 0))
    return games


async def fetch_stats_box(
    http: httpx.AsyncClient, game_sno: int | str, year: int
) -> dict[str, Any] | None:
    response = await http.get(
        f"{STATS_BASE}/schedule/{year}-A-{game_sno}",
        headers=STATS_HEADERS,
    )
    if response.status_code != 200:
        return None

    html = response.text
    title_match = _TITLE_SCORE_RE.search(html)
    vs_match = _TITLE_VS_RE.search(html)
    inning_blocks = _parse_inning_blocks(html)
    if len(inning_blocks) < 2 and not title_match:
        parsed = _parse_stats_game_page(html, game_sno=int(game_sno), year=year)
        if not parsed or not vs_match:
            return None
        away_starter, home_starter = parse_stats_starters(html, game_sno=int(game_sno), year=year)
        pitching = parse_stats_pitching(html, game_sno=int(game_sno), year=year)
        payload = {
            "gameSno": int(game_sno),
            "year": year,
            "awayInnings": [0] * 9,
            "homeInnings": [0] * 9,
            "awayStarter": away_starter,
            "homeStarter": home_starter,
            "pitching": pitching,
            "batting": [],
            "firstSno": [],
            "liveLog": [],
            "scoreboard": [],
            "awayTeamId": parsed["awayTeamId"],
            "homeTeamId": parsed["homeTeamId"],
            "awayScore": 0,
            "homeScore": 0,
        }
        attach_stats_lineup_data(payload, html, game_sno=int(game_sno), year=year)
        return payload

    if len(inning_blocks) < 2:
        parsed = _parse_stats_game_page(html, game_sno=int(game_sno), year=year)
        if not parsed:
            return None
        away_starter, home_starter = parse_stats_starters(html, game_sno=int(game_sno), year=year)
        pitching = parse_stats_pitching(html, game_sno=int(game_sno), year=year)
        payload = {
            "gameSno": int(game_sno),
            "year": year,
            "awayInnings": [0] * 9,
            "homeInnings": [0] * 9,
            "awayStarter": away_starter,
            "homeStarter": home_starter,
            "pitching": pitching,
            "batting": [],
            "firstSno": [],
            "liveLog": [],
            "scoreboard": [],
            "awayTeamId": parsed["awayTeamId"],
            "homeTeamId": parsed["homeTeamId"],
            "awayScore": parsed.get("awayScore") or 0,
            "homeScore": parsed.get("homeScore") or 0,
        }
        attach_stats_lineup_data(payload, html, game_sno=int(game_sno), year=year)
        return payload

    away_innings, home_innings = inning_blocks[0], inning_blocks[1]
    away_team = home_team = None
    away_score = home_score = None
    if title_match:
        away_team = match_team(title_match.group("away"))
        home_team = match_team(title_match.group("home"))
        away_score = int(title_match.group("away_score"))
        home_score = int(title_match.group("home_score"))

    parsed = _parse_stats_game_page(html, game_sno=int(game_sno), year=year)
    if (not away_team or not home_team) and parsed:
        away_team = away_team or match_team(parsed.get("awayNameZh") or "")
        home_team = home_team or match_team(parsed.get("homeNameZh") or "")
        if away_score is None:
            away_score = parsed.get("awayScore")
        if home_score is None:
            home_score = parsed.get("homeScore")

    if not away_team or not home_team:
        return None

    away_starter, home_starter = parse_stats_starters(html, game_sno=int(game_sno), year=year)
    pitching = parse_stats_pitching(html, game_sno=int(game_sno), year=year)

    payload = {
        "gameSno": int(game_sno),
        "year": year,
        "awayInnings": away_innings,
        "homeInnings": home_innings,
        "awayStarter": away_starter,
        "homeStarter": home_starter,
        "pitching": pitching,
        "batting": [],
        "firstSno": [],
        "liveLog": [],
        "scoreboard": [],
        "awayTeamId": away_team["id"],
        "homeTeamId": home_team["id"],
        "awayScore": away_score if away_score is not None else sum(away_innings),
        "homeScore": home_score if home_score is not None else sum(home_innings),
    }
    attach_stats_lineup_data(payload, html, game_sno=int(game_sno), year=year)
    return payload
