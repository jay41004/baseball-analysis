"""NPB data from npb.jp schedule and score pages."""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.inning_comparison import build_inning_comparison, build_matchup_situational, strip_panel_internals
from app.npb_teams import TEAM_BY_ID, list_teams, match_team, team_zh

NPB_BASE = "https://npb.jp"
GAME_PARSER_VERSION = 5
JST = timezone(timedelta(hours=9))
# npb.jp lists matchups as HOME - AWAY (e.g. 巨人 - 中日 at 東京ドーム).
FINAL_SCORE_RE = re.compile(
    r"^(?P<home>.+?)\s+(?P<homeScore>\d+)\s*-\s*(?P<awayScore>\d+)\s+(?P<away>.+)$"
)
UPCOMING_RE = re.compile(r"^(?P<home>.+?)\s*-\s*(?P<away>.+)$")
DATE_RE = re.compile(r"^(?P<month>\d+)/(?P<day>\d+)（")
STARTER_RE = re.compile(r"先発[：:]\s*([^\s先発]+)")
PROBABLE_RE = re.compile(r"(?:\(予\)|先発[：:])\s*([^\s(先発]+)")
PBP_STARTER_RE = re.compile(r"（先発投手）(.+?)(?:\s|$|（|）)")
PBP_CHANGE_RE = re.compile(r"（投手交代）(.+?)→(.+?)(?:\s|$|（|）)")
PBP_RBI_RE = re.compile(r"打点(\d+)")
PBP_BATTER_NAME_RE = re.compile(
    r"^\dアウト(?:\s+(?:\d+(?:・\d+)*)?塁)*\s+(\S+)\s+\d-\d"
)
NPB_HIT_TERMS = ("ヒット", "ホームラン", "ホームラン", "犠牲フライ")


class NpbClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NPB-Analytics/1.0)"},
            follow_redirects=True,
        )
        self._schedule_cache: list[dict[str, Any]] | None = None
        self._game_cache: dict[str, dict[str, Any]] = {}
        self._playbyplay_cache: dict[str, str] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch_schedule(self, months_back: int = 6) -> list[dict[str, Any]]:
        if self._schedule_cache is not None:
            return self._schedule_cache

        today = date.today()
        month_pairs: list[tuple[int, int]] = []
        year = today.year
        month = today.month
        for _ in range(months_back + 2):
            month_pairs.append((year, month))
            month -= 1
            if month == 0:
                month = 12
                year -= 1

        games: list[dict[str, Any]] = []
        for year, month in reversed(month_pairs):
            url = f"{NPB_BASE}/games/{year}/schedule_{month:02d}_detail.html"
            try:
                resp = await self._http.get(url)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            games.extend(self._parse_schedule_page(resp.text, year))

        games.sort(key=lambda g: (g.get("date", ""), g.get("startTime", ""), g.get("href") or ""))
        self._schedule_cache = games
        return games

    def _parse_schedule_page(self, html: str, year: int) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        rows: list[dict[str, Any]] = []
        current_date = ""

        for tr in soup.select("table tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.select("th, td")]
            if not cells or cells[0] == "月日":
                continue

            if DATE_RE.match(cells[0]):
                current_date = cells[0]
                cells = cells[1:]
            if not cells or not cells[0]:
                continue

            matchup_text = cells[0]
            if matchup_text in {"(予備日)", "オールスターゲーム"}:
                continue

            stadium = cells[1] if len(cells) > 1 else ""
            pitchers = cells[-1] if len(cells) > 2 else ""
            link = tr.select_one('a[href*="/scores/"]')
            href = link.get("href") if link else None
            iso_date = self._parse_schedule_date(current_date, year)
            start_time = self._parse_start_time(stadium)
            parsed = self._parse_matchup_text(matchup_text)
            if not parsed:
                continue

            away = parsed["away"]
            home = parsed["home"]
            status = parsed["status"]
            # npb.jp lists probable pitchers in HOME then AWAY order (same as team names).
            home_starter, away_starter = self._parse_probable_pitchers(pitchers)

            rows.append(
                {
                    "date": iso_date,
                    "dateLabel": current_date,
                    "matchupText": matchup_text,
                    "awayTeamId": away["id"],
                    "homeTeamId": home["id"],
                    "awayNameZh": away["nameZh"],
                    "homeNameZh": home["nameZh"],
                    "awayScore": parsed.get("awayScore"),
                    "homeScore": parsed.get("homeScore"),
                    "status": status,
                    "stadium": stadium,
                    "startTime": start_time,
                    "href": href,
                    "pitchersNote": pitchers,
                    "awayProbablePitcher": away_starter,
                    "homeProbablePitcher": home_starter,
                }
            )
        return rows

    def _parse_schedule_date(self, label: str, year: int) -> str:
        match = DATE_RE.match(label)
        if not match:
            return ""
        return f"{year}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"

    def _parse_start_time(self, stadium: str) -> str:
        match = re.search(r"(\d{1,2}:\d{2})", stadium)
        return match.group(1) if match else ""

    def _parse_matchup_text(self, text: str) -> dict[str, Any] | None:
        if "中止" in text or "ノーゲーム" in text:
            return None

        final = FINAL_SCORE_RE.match(text)
        if final:
            home = match_team(final.group("home"))
            away = match_team(final.group("away"))
            if not away or not home:
                return None
            return {
                "away": away,
                "home": home,
                "awayScore": int(final.group("awayScore")),
                "homeScore": int(final.group("homeScore")),
                "status": "Final",
            }

        upcoming = UPCOMING_RE.match(text)
        if upcoming:
            home = match_team(upcoming.group("home"))
            away = match_team(upcoming.group("away"))
            if not away or not home:
                return None
            return {"away": away, "home": home, "status": "Scheduled"}

        return None

    def _parse_probable_pitchers(self, note: str) -> tuple[str | None, str | None]:
        """Return (home_starter, away_starter) in npb.jp schedule order."""
        if not note:
            return None, None
        starters = STARTER_RE.findall(note)
        if len(starters) >= 2:
            return starters[0], starters[1]
        probable = PROBABLE_RE.findall(note)
        if len(probable) >= 2:
            return probable[0], probable[1]
        if len(probable) == 1:
            return probable[0], None
        return None, None

    async def fetch_game(self, href: str) -> dict[str, Any] | None:
        cache_key = f"{GAME_PARSER_VERSION}:{href}"
        if cache_key in self._game_cache:
            return self._game_cache[cache_key]

        url = href if href.startswith("http") else f"{NPB_BASE}{href}"
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError:
            return None

        parsed = self._parse_game_page(resp.text, href)
        if parsed:
            self._game_cache[cache_key] = parsed
        return parsed

    def _playbyplay_url(self, href: str) -> str:
        if href.startswith("http"):
            base = href.rstrip("/")
        else:
            base = f"{NPB_BASE}{href}".rstrip("/")
        if base.endswith(".html"):
            base = base.rsplit("/", 1)[0]
        return f"{base}/playbyplay.html"

    async def fetch_playbyplay(self, href: str) -> str | None:
        url = self._playbyplay_url(href)
        if url in self._playbyplay_cache:
            return self._playbyplay_cache[url]
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError:
            return None
        self._playbyplay_cache[url] = resp.text
        return resp.text

    def _parse_game_page(self, html: str, href: str) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("div#table_linescore")
        if not table:
            return None

        away_row = table.select_one("tr.top")
        home_row = table.select_one("tr.bottom")
        if not away_row or not home_row:
            return None

        away_cells = [cell.get_text(strip=True) for cell in away_row.select("th, td")]
        home_cells = [cell.get_text(strip=True) for cell in home_row.select("th, td")]
        if len(away_cells) < 2 or len(home_cells) < 2:
            return None

        away_team = match_team(away_cells[0])
        home_team = match_team(home_cells[0])
        if not away_team or not home_team:
            return None

        away_innings = self._parse_inning_cells(away_cells[1:])
        home_innings = self._parse_inning_cells(home_cells[1:])
        starters = self._parse_starters(soup)

        return {
            "href": href,
            "awayTeamId": away_team["id"],
            "homeTeamId": home_team["id"],
            "awayInnings": away_innings,
            "homeInnings": home_innings,
            "awayStarter": starters.get(away_team["id"]),
            "homeStarter": starters.get(home_team["id"]),
        }

    def _parse_inning_cells(self, cells: list[str]) -> list[int]:
        runs: list[int] = []
        for value in cells:
            if value in {"計", "H", "E"}:
                break
            if value in {"x", "X", "-", ""}:
                runs.append(0)
            elif value.isdigit():
                runs.append(int(value))
            else:
                runs.append(0)
        return runs

    def _parse_starters(self, soup: BeautifulSoup) -> dict[int, str]:
        starters: dict[int, str] = {}
        for row in soup.select("table tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
            if len(cells) < 2:
                continue
            label, pitchers = cells[0], cells[1]
            if not label.startswith("【") or "】" not in label:
                continue
            if label in {"【勝投手】", "【敗投手】", "【セーブ】"}:
                continue
            team_label = label.strip("【】")
            team = match_team(team_label)
            if not team:
                continue
            starter = _starter_from_lineup(pitchers)
            if starter and team["id"] not in starters:
                starters[team["id"]] = starter
        return starters


def _normalize_pitcher_name(name: str) -> str:
    return name.replace(" ", "").replace("　", "").strip()


def _pitcher_name_matches(probable: str, starter: str) -> bool:
    left = _normalize_pitcher_name(probable)
    right = _normalize_pitcher_name(starter)
    if not left or not right:
        return False
    return left in right or right in left


def _is_pitching_lineup(text: str) -> bool:
    if not text or not text.strip():
        return False
    if "号" in text and "ラン" in text:
        return False
    return "、" in text


def _starter_from_lineup(text: str) -> str | None:
    if not _is_pitching_lineup(text):
        return None
    first = text.split("、")[0].strip()
    return first or None


def first_n_runs(inning_runs: list[int], count: int = 5) -> int:
    return sum(inning_runs[:count])


def scored_innings_from_runs(runs_by_inning: list[int]) -> list[int]:
    return [index + 1 for index, runs in enumerate(runs_by_inning) if runs > 0]


def opponent_runs_by_inning(opp_innings: list[int], max_inning: int = 9) -> list[int]:
    return [(opp_innings[index] if index < len(opp_innings) else 0) for index in range(max_inning)]


def _defensive_half_label(is_home: bool) -> str:
    # Away team pitches the bottom half; home team pitches the top half.
    return "表" if is_home else "裏"


def _parse_pitcher_runs_from_playbyplay(
    html: str,
    is_home: bool,
    pitcher_name: str,
    opp_innings: list[int] | None = None,
) -> list[int]:
    """Runs allowed by pitcher in each inning (index 0 = inning 1) from npb.jp play-by-play."""
    soup = BeautifulSoup(html, "html.parser")
    defend_half = _defensive_half_label(is_home)
    runs = [0] * 9
    current_pitcher: str | None = None
    opp = opponent_runs_by_inning(opp_innings or [])

    for heading in soup.find_all("h5"):
        title = heading.get_text(strip=True)
        match = re.match(r"(\d+)回(表|裏)", title)
        if not match:
            continue
        inning = int(match.group(1))
        half = match.group(2)
        if half != defend_half or inning < 1 or inning > 9:
            continue

        half_had_change = False
        for element in heading.find_all_next(["tr", "h5"]):
            if element.name == "h5":
                break
            row = element.get_text(" ", strip=True)
            if not row:
                continue

            starter = PBP_STARTER_RE.search(row)
            if starter:
                current_pitcher = starter.group(1).strip()
                continue

            change = PBP_CHANGE_RE.search(row)
            if change:
                half_had_change = True
                outgoing = change.group(1).strip()
                incoming = change.group(2).strip()
                if current_pitcher and _pitcher_name_matches(pitcher_name, outgoing):
                    runs[inning - 1] += sum(int(value) for value in PBP_RBI_RE.findall(row))
                current_pitcher = incoming
                continue

            if current_pitcher and _pitcher_name_matches(pitcher_name, current_pitcher):
                runs[inning - 1] += sum(int(value) for value in PBP_RBI_RE.findall(row))

        # npb.jp omits 打点 on some scoring plays (e.g. GIDP run). When the pitcher
        # worked the entire defensive half with no mid-inning change, use linescore.
        if (
            not half_had_change
            and current_pitcher
            and _pitcher_name_matches(pitcher_name, current_pitcher)
        ):
            runs[inning - 1] = opp[inning - 1]

    return runs


def summarize_thresholds(runs_list: list[int]) -> dict[str, Any]:
    total = len(runs_list)
    return {
        "totalGames": total,
        "over15": sum(1 for runs in runs_list if runs > 1.5),
        "under15": sum(1 for runs in runs_list if runs <= 1.5),
        "over25": sum(1 for runs in runs_list if runs > 2.5),
        "under25": sum(1 for runs in runs_list if runs <= 2.5),
        "avgRuns": round(sum(runs_list) / total, 2) if total else 0,
    }


def summarize_team_scoring(rows: list[dict[str, Any]], runs_list: list[int]) -> dict[str, Any]:
    total = len(rows)
    first_inning_scored = sum(1 for row in rows if row.get("firstInningScored"))
    return {
        **summarize_thresholds(runs_list),
        "firstInningScored": first_inning_scored,
        "firstInningNoScore": total - first_inning_scored,
    }


def summarize_pitcher_summary(rows: list[dict[str, Any]], runs_list: list[int]) -> dict[str, Any]:
    total = len(rows)
    inning_scored_counts = {str(inning): 0 for inning in range(1, 10)}
    for row in rows:
        scored = set(row.get("scoredInnings") or [])
        if row.get("firstInningScored"):
            scored.add(1)
        for inning in scored:
            if 1 <= inning <= 9:
                inning_scored_counts[str(inning)] += 1
    first_inning_scored = inning_scored_counts["1"]
    return {
        **summarize_thresholds(runs_list),
        "firstInningScored": first_inning_scored,
        "firstInningClean": total - first_inning_scored,
        "inningScoredCounts": inning_scored_counts,
    }


async def fetch_npb_teams() -> list[dict[str, Any]]:
    return list_teams()


async def fetch_next_matchup(client: NpbClient, focus_team_id: int) -> dict[str, Any] | None:
    schedule = await client.fetch_schedule()
    today = datetime.now(JST).date().isoformat()

    upcoming = [
        game
        for game in schedule
        if focus_team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game["status"] != "Final"
        and game.get("date", "") >= today
    ]
    if not upcoming:
        return None

    def sort_key(game: dict[str, Any]) -> tuple[str, int, str]:
        game_date = game.get("date") or "9999-99-99"
        start = game.get("startTime") or "99:99"
        has_pitchers = int(not (game.get("awayProbablePitcher") and game.get("homeProbablePitcher")))
        return (game_date, has_pitchers, start)

    upcoming.sort(key=sort_key)
    game = upcoming[0]
    focus_is_home = game["homeTeamId"] == focus_team_id

    def side_info(team_id: int, probable: str | None) -> dict[str, Any]:
        return {
            "teamId": team_id,
            "teamName": team_zh(team_id),
            "probablePitcher": {"fullName": probable} if probable else None,
        }

    return {
        "date": game.get("date"),
        "gameDate": f"{game.get('date')}T{game.get('startTime') or '18:00'}:00+09:00",
        "status": game.get("status"),
        "stadium": game.get("stadium"),
        "href": game.get("href"),
        "awayTeamId": game["awayTeamId"],
        "homeTeamId": game["homeTeamId"],
        "focusTeamId": focus_team_id,
        "away": side_info(game["awayTeamId"], game.get("awayProbablePitcher")),
        "home": side_info(game["homeTeamId"], game.get("homeProbablePitcher")),
        "focusIsHome": focus_is_home,
    }


async def analyze_team_scoring(
    client: NpbClient, team_id: int, game_count: int = 10
) -> dict[str, Any]:
    team = TEAM_BY_ID[team_id]
    schedule = await client.fetch_schedule()
    pool = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("href")
    ]
    pool.sort(key=lambda g: g.get("date", ""), reverse=True)
    pool = pool[:50]

    if not pool:
        return {
            "teamId": team_id,
            "teamName": team["nameZh"],
            "games": [],
            "_scoredPool": [],
            "summary": summarize_team_scoring([], []),
        }

    panel_meta = pool[:game_count]
    away_meta = [game for game in pool if game["homeTeamId"] != team_id][:10]
    home_meta = [game for game in pool if game["homeTeamId"] == team_id][:10]

    needed_meta: list[dict[str, Any]] = []
    seen_hrefs: set[str] = set()
    for meta in panel_meta + away_meta + home_meta:
        href = meta.get("href")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        needed_meta.append(meta)

    parsed_list = await asyncio.gather(*[client.fetch_game(meta["href"]) for meta in needed_meta])
    parsed_by_href = {
        meta["href"]: parsed for meta, parsed in zip(needed_meta, parsed_list) if parsed
    }

    def build_row(meta: dict[str, Any], parsed: dict[str, Any], *, include_scored: bool = False) -> dict[str, Any]:
        is_home = parsed["homeTeamId"] == team_id
        side = "home" if is_home else "away"
        opp_side = "away" if is_home else "home"
        inning_runs = parsed[f"{side}Innings"]
        first_inning = inning_runs[0] if inning_runs else 0
        first_five = first_n_runs(inning_runs, 5)
        opponent_id = parsed[f"{opp_side}TeamId"]
        opponent_starter = parsed.get(f"{opp_side}Starter")
        team_score = meta.get("homeScore" if is_home else "awayScore")
        opponent_score = meta.get("awayScore" if is_home else "homeScore")
        if team_score is None or opponent_score is None:
            team_score = sum(parsed[f"{side}Innings"])
            opponent_score = sum(parsed[f"{opp_side}Innings"])

        row = {
            "date": meta.get("date"),
            "opponent": team_zh(opponent_id),
            "opponentStarter": opponent_starter,
            "isHome": is_home,
            "teamScore": team_score,
            "opponentScore": opponent_score,
            "firstInningRuns": first_inning,
            "firstInningScored": first_inning > 0,
            "firstFiveRuns": first_five,
            "over15": first_five > 1.5,
            "over25": first_five > 2.5,
        }
        if include_scored:
            scored_innings: list[int] = []
            for index in range(9):
                runs = inning_runs[index] if index < len(inning_runs) else 0
                if runs > 0:
                    scored_innings.append(index + 1)
            row["scoredInnings"] = scored_innings
        return row

    scored_pool: list[dict[str, Any]] = []
    pool_seen: set[str] = set()
    for meta in away_meta + home_meta:
        href = meta.get("href")
        if not href or href in pool_seen:
            continue
        parsed = parsed_by_href.get(href)
        if not parsed:
            continue
        pool_seen.add(href)
        scored_pool.append(build_row(meta, parsed, include_scored=True))

    rows: list[dict[str, Any]] = []
    for meta in panel_meta:
        parsed = parsed_by_href.get(meta.get("href", ""))
        if parsed:
            rows.append(build_row(meta, parsed))

    runs_list = [row["firstFiveRuns"] for row in rows]
    return {
        "teamId": team_id,
        "teamName": team["nameZh"],
        "games": rows,
        "_scoredPool": scored_pool,
        "summary": summarize_team_scoring(rows, runs_list),
    }


def _build_pitcher_start_row(
    meta: dict[str, Any],
    parsed: dict[str, Any],
    team_id: int,
    pitcher_name: str,
    *,
    pitcher_runs_by_inning: list[int] | None = None,
) -> dict[str, Any]:
    is_home = parsed["homeTeamId"] == team_id
    side = "home" if is_home else "away"
    opp_innings = parsed["awayInnings" if is_home else "homeInnings"]
    if pitcher_runs_by_inning is not None:
        runs_by_inning = pitcher_runs_by_inning[:9]
        while len(runs_by_inning) < 9:
            runs_by_inning.append(0)
        first_five_allowed = first_n_runs(runs_by_inning, 5)
    else:
        runs_by_inning = opponent_runs_by_inning(opp_innings)
        first_five_allowed = first_n_runs(opp_innings, 5)
    first_inning_allowed = runs_by_inning[0]
    opponent_id = parsed["awayTeamId" if is_home else "homeTeamId"]
    opp_side = "away" if is_home else "home"
    team_score = meta.get("homeScore" if is_home else "awayScore")
    opponent_score = meta.get("awayScore" if is_home else "homeScore")
    if team_score is None or opponent_score is None:
        team_score = sum(parsed[f"{side}Innings"])
        opponent_score = sum(parsed[f"{opp_side}Innings"])
    return {
        "date": meta.get("date"),
        "opponent": team_zh(opponent_id),
        "opponentStarter": parsed.get(f"{'away' if is_home else 'home'}Starter"),
        "isHome": is_home,
        "teamScore": team_score,
        "opponentScore": opponent_score,
        "firstInningRunsAllowed": first_inning_allowed,
        "firstInningScored": first_inning_allowed > 0,
        "runsByInning": runs_by_inning,
        "scoredInnings": scored_innings_from_runs(runs_by_inning),
        "firstFiveRunsAllowed": first_five_allowed,
        "over15": first_five_allowed > 1.5,
        "over25": first_five_allowed > 2.5,
    }


async def analyze_pitcher_starts(
    client: NpbClient,
    pitcher_name: str,
    team_id: int,
    game_count: int = 10,
    *,
    scan_limit: int = 40,
) -> dict[str, Any]:
    if not pitcher_name:
        empty = summarize_pitcher_summary([], [])
        return {"pitcherName": pitcher_name, "games": [], "summary": empty}

    schedule = await client.fetch_schedule()
    candidates = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("href")
    ]
    candidates.sort(key=lambda g: g.get("date", ""), reverse=True)

    async def try_game(meta: dict[str, Any]) -> dict[str, Any] | None:
        parsed = await client.fetch_game(meta["href"])
        if not parsed:
            return None
        is_home = parsed["homeTeamId"] == team_id
        side = "home" if is_home else "away"
        starter = parsed.get(f"{side}Starter")
        if not starter or not _pitcher_name_matches(pitcher_name, starter):
            return None
        pbp_html = await client.fetch_playbyplay(meta["href"])
        opp_innings = parsed["awayInnings" if is_home else "homeInnings"]
        pbp_runs = (
            _parse_pitcher_runs_from_playbyplay(
                pbp_html, is_home, pitcher_name, opp_innings
            )
            if pbp_html
            else None
        )
        return _build_pitcher_start_row(
            meta,
            parsed,
            team_id,
            pitcher_name,
            pitcher_runs_by_inning=pbp_runs,
        )

    rows: list[dict[str, Any]] = []
    batch_size = 15
    for index in range(0, len(candidates), batch_size):
        if len(rows) >= scan_limit:
            break
        batch = candidates[index : index + batch_size]
        results = await asyncio.gather(*[try_game(meta) for meta in batch])
        rows.extend(row for row in results if row)

    rows.sort(key=lambda row: row.get("date", ""), reverse=True)
    rows = rows[:scan_limit]
    display_rows = rows[:game_count]

    runs_list = [row["firstFiveRunsAllowed"] for row in display_rows]
    return {
        "pitcherName": pitcher_name,
        "games": display_rows,
        "_startPool": rows,
        "summary": summarize_pitcher_summary(display_rows, runs_list),
    }


async def fetch_inning_comparison(
    client: NpbClient, team_id: int, *, game_count: int = 20
) -> dict[str, Any]:
    team = TEAM_BY_ID[team_id]
    schedule = await client.fetch_schedule()
    finished = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("href")
    ]
    finished.sort(key=lambda game: game.get("date", ""), reverse=True)
    finished = finished[:game_count]

    rows: list[dict[str, Any]] = []
    if finished:
        parsed_list = await asyncio.gather(
            *[client.fetch_game(meta["href"]) for meta in finished]
        )
        for meta, parsed in zip(finished, parsed_list):
            if not parsed:
                continue

            is_home = parsed["homeTeamId"] == team_id
            side = "home" if is_home else "away"
            opp_side = "away" if is_home else "home"
            my_innings = parsed[f"{side}Innings"]
            opp_innings = parsed[f"{opp_side}Innings"]

            scored_innings: list[int] = []
            allowed_innings: list[int] = []
            for index in range(9):
                runs = my_innings[index] if index < len(my_innings) else 0
                runs_allowed = opp_innings[index] if index < len(opp_innings) else 0
                inning = index + 1
                if runs > 0:
                    scored_innings.append(inning)
                if runs_allowed > 0:
                    allowed_innings.append(inning)

            rows.append(
                {
                    "scoredInnings": scored_innings,
                    "allowedInnings": allowed_innings,
                }
            )

    return build_inning_comparison(team["nameZh"], rows)


def _batting_half_for_team(*, team_id: int, away_team_id: int, home_team_id: int) -> str:
    return "裏" if team_id == home_team_id else "表"


def _extract_batter_name_from_pbp_row(row: str) -> str | None:
    if "先発投手" in row or "より" not in row:
        return None
    match = PBP_BATTER_NAME_RE.search(row)
    if not match:
        return None
    name = match.group(1).strip()
    if name.endswith("塁") or re.match(r"\d-\d", name):
        return None
    return name


def _parse_first_inning_batters(pbp_html: str, *, batting_half: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(pbp_html, "html.parser")
    batters: list[dict[str, Any]] = []
    seen: set[str] = set()
    in_first = False

    for heading in soup.find_all("h5"):
        title = heading.get_text(strip=True)
        if title.startswith("1回") and batting_half in title:
            in_first = True
        elif in_first and title.startswith("2回"):
            break
        if not in_first:
            continue

        for element in heading.find_all_next(["tr", "h5"]):
            if element.name == "h5":
                break
            row = element.get_text(" ", strip=True)
            name = _extract_batter_name_from_pbp_row(row)
            if not name or name in seen:
                continue
            seen.add(name)
            batters.append({"order": len(batters) + 1, "name": name, "position": ""})
            if len(batters) >= 9:
                return batters
    return batters


def _batter_had_hit_in_row(row: str, batter_name: str) -> bool:
    if batter_name not in row:
        return False
    return any(term in row for term in NPB_HIT_TERMS)


def _recent_batting_form_from_pbps(
    pbps: list[tuple[str, int, int]], batter_name: str, *, team_id: int
) -> dict[str, Any]:
    logs: list[dict[str, int]] = []
    for pbp_html, away_id, home_id in pbps:
        half = _batting_half_for_team(
            team_id=team_id, away_team_id=away_id, home_team_id=home_id
        )
        soup = BeautifulSoup(pbp_html, "html.parser")
        plate_appearances = 0
        had_hit = False
        for heading in soup.find_all("h5"):
            title = heading.get_text(strip=True)
            if not re.match(r"\d+回", title) or half not in title:
                continue
            for element in heading.find_all_next(["tr", "h5"]):
                if element.name == "h5":
                    break
                row = element.get_text(" ", strip=True)
                if _extract_batter_name_from_pbp_row(row) == batter_name:
                    plate_appearances += 1
                    if _batter_had_hit_in_row(row, batter_name):
                        had_hit = True
        if plate_appearances:
            logs.append({"hits": int(had_hit), "atBats": plate_appearances})

    recent3 = logs[:3]
    recent5 = logs[:5]

    def totals(games: list[dict[str, int]]) -> tuple[int, int, int]:
        hit_games = sum(game["hits"] for game in games)
        hits = hit_games
        at_bats = sum(game["atBats"] for game in games)
        return hit_games, hits, at_bats

    hit_games_3, hits_3, ab_3 = totals(recent3)
    _, hits_5, ab_5 = totals(recent5)

    def format_avg(hits: int, at_bats: int) -> str | None:
        if at_bats <= 0:
            return None
        text = f"{hits / at_bats:.3f}"
        return text[1:] if text.startswith("0.") else text

    return {
        "recent3HitGames": hit_games_3,
        "recent3Games": len(recent3),
        "recent3Avg": format_avg(hits_3, ab_3),
        "recent5Avg": format_avg(hits_5, ab_5),
    }


async def _lineup_for_team(
    client: NpbClient, team_id: int, *, preferred_href: str | None = None
) -> tuple[list[dict[str, Any]], str, str | None]:
    schedule = await client.fetch_schedule()
    candidates: list[dict[str, Any]] = []
    if preferred_href:
        candidates.append({"href": preferred_href, "date": None})
    candidates.extend(
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("href")
    )
    candidates.sort(key=lambda game: game.get("date") or "", reverse=True)

    for meta in candidates[:6]:
        href = meta.get("href")
        if not href:
            continue
        parsed = await client.fetch_game(href)
        pbp = await client.fetch_playbyplay(href)
        if not parsed or not pbp:
            continue
        half = _batting_half_for_team(
            team_id=team_id,
            away_team_id=parsed["awayTeamId"],
            home_team_id=parsed["homeTeamId"],
        )
        batters = _parse_first_inning_batters(pbp, batting_half=half)
        if batters:
            source = "confirmed" if href == preferred_href else "previous"
            source_date = meta.get("date") or parsed.get("date")
            recent_meta = [
                game
                for game in schedule
                if game["status"] == "Final"
                and team_id in {game["awayTeamId"], game["homeTeamId"]}
                and game.get("href")
            ]
            recent_meta.sort(key=lambda game: game.get("date", ""), reverse=True)
            recent_meta = recent_meta[:5]
            pbps: list[tuple[str, int, int]] = []
            for recent in recent_meta:
                recent_pbp = await client.fetch_playbyplay(recent["href"])
                recent_parsed = await client.fetch_game(recent["href"])
                if recent_pbp and recent_parsed:
                    pbps.append(
                        (
                            recent_pbp,
                            recent_parsed["awayTeamId"],
                            recent_parsed["homeTeamId"],
                        )
                    )
            enriched: list[dict[str, Any]] = []
            for batter in batters:
                copy = dict(batter)
                copy.update(_recent_batting_form_from_pbps(pbps, copy["name"], team_id=team_id))
                enriched.append(copy)
            return enriched, source, source_date
    return [], "previous", None


def _resolve_opposing_pitcher(matchup: dict[str, Any], *, side_key: str) -> dict[str, Any] | None:
    opposing_key = "home" if side_key == "away" else "away"
    return matchup.get(opposing_key, {}).get("probablePitcher")


async def fetch_matchup_starting_lineups(
    client: NpbClient, matchup: dict[str, Any]
) -> dict[str, Any]:
    preferred_href = matchup.get("href")
    lineups: dict[str, Any] = {}
    for side_key in ("away", "home"):
        side_info = matchup[side_key]
        team_id = side_info["teamId"]
        batters, source, source_date = await _lineup_for_team(
            client, team_id, preferred_href=preferred_href
        )
        lineups[side_key] = {
            "teamName": side_info["teamName"],
            "source": source,
            "sourceDate": source_date or matchup.get("date"),
            "opposingPitcher": _resolve_opposing_pitcher(matchup, side_key=side_key),
            "batters": batters,
        }
    return lineups


async def analyze_matchup_a_table(focus_team_id: int) -> dict[str, Any]:
    client = NpbClient()
    try:
        matchup = await fetch_next_matchup(client, focus_team_id)
        if not matchup:
            raise ValueError("找不到下一場比賽")
        away_table, home_table = await asyncio.gather(
            fetch_inning_comparison(client, matchup["away"]["teamId"]),
            fetch_inning_comparison(client, matchup["home"]["teamId"]),
        )
    finally:
        await client.close()
    return {"away": away_table, "home": home_table}


async def _build_side_panel(
    client: NpbClient, side_info: dict[str, Any], game_count: int
) -> dict[str, Any]:
    scoring = await analyze_team_scoring(client, side_info["teamId"], game_count)
    probable = side_info.get("probablePitcher")
    pitcher_analysis = None
    if probable and probable.get("fullName"):
        pitcher_analysis = await analyze_pitcher_starts(
            client, probable["fullName"], side_info["teamId"], game_count, scan_limit=40
        )

    return {
        **scoring,
        "probablePitcher": probable,
        "pitcherAnalysis": pitcher_analysis,
    }


async def analyze_matchup(focus_team_id: int, game_count: int = 10) -> dict[str, Any]:
    client = NpbClient()
    try:
        matchup = await fetch_next_matchup(client, focus_team_id)
        if not matchup:
            raise ValueError("找不到下一場比賽")

        away_id = matchup["away"]["teamId"]
        home_id = matchup["home"]["teamId"]
        away_panel, home_panel, away_table, home_table = await asyncio.gather(
            _build_side_panel(client, matchup["away"], game_count),
            _build_side_panel(client, matchup["home"], game_count),
            fetch_inning_comparison(client, away_id),
            fetch_inning_comparison(client, home_id),
        )
        situational = build_matchup_situational(away_panel, home_panel)
        away_panel = strip_panel_internals(away_panel)
        home_panel = strip_panel_internals(home_panel)
        starting_lineups = {"away": {"batters": []}, "home": {"batters": []}}
    finally:
        await client.close()

    return {
        "focusTeamId": focus_team_id,
        "matchup": {
            "date": matchup.get("date"),
            "gameDate": matchup.get("gameDate"),
            "status": matchup.get("status"),
            "stadium": matchup.get("stadium"),
        },
        "away": away_panel,
        "home": home_panel,
        "aTable": {"away": away_table, "home": home_table},
        "startingLineups": starting_lineups,
        "situational": situational,
    }
