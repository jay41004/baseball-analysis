"""CPBL data from cpbl.com.tw schedule and box score APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.cpbl_season_batting import (
    format_avg,
    format_ops,
    get_season_batting_lookup,
    schedule_season_batting_refresh,
)
from app.cpbl_risp_batting import get_risp_lookup, lookup_risp_avg, schedule_risp_refresh
from app.cpbl_vs_pitcher import (
    get_vs_pitcher_lookup,
    lookup_vs_pitcher_avgs,
    parse_pitcher_acnt_map,
    schedule_vs_pitcher_refresh,
)
from app.cpbl_stats import fetch_stats_box, fetch_stats_schedule
from app.cpbl_teams import TEAM_BY_ID, list_teams, team_by_code, team_zh
from app.inning_comparison import (
    build_inning_comparison,
    build_matchup_situational,
    strip_panel_internals,
)

logger = logging.getLogger(__name__)

CPBL_BASE = "https://www.cpbl.com.tw"
SCHEDULE_PAGE = f"{CPBL_BASE}/schedule"
KIND_CODE = "A"
TPE = timezone(timedelta(hours=8))
_FETCH_SEM = asyncio.Semaphore(8)

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEDULE_CACHE_FILE = BASE_DIR / "data" / "cpbl_schedule.json"
SCHEDULE_DISK_TTL = timedelta(hours=6)

_shared_schedule: list[dict[str, Any]] | None = None
_shared_box_cache: dict[str, dict[str, Any]] = {}
_schedule_lock = asyncio.Lock()

CSRF_JS = re.compile(r"RequestVerificationToken:\s*['\"]([^'\"]+)['\"]")
CSRF_INPUT = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')

POSITION_BY_CODE: dict[str, str] = {
    "1": "P",
    "2": "C",
    "3": "1B",
    "4": "2B",
    "5": "3B",
    "6": "SS",
    "7": "LF",
    "8": "CF",
    "9": "RF",
    "10": "DH",
    "投手": "P",
    "捕手": "C",
    "一壘手": "1B",
    "二壘手": "2B",
    "三壘手": "3B",
    "游擊手": "SS",
    "左外野手": "LF",
    "中外野手": "CF",
    "右外野手": "RF",
    "指定打擊": "DH",
}

POSITION_ZH_SHORT: dict[str, str] = {
    "P": "投",
    "C": "捕",
    "1B": "一",
    "2B": "二",
    "3B": "三",
    "SS": "游",
    "LF": "左",
    "CF": "中",
    "RF": "右",
    "DH": "DH",
}


def _parse_json_blob(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        if not value.strip():
            return []
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


def _extract_csrf(html: str) -> str | None:
    match = CSRF_JS.search(html)
    if match:
        return match.group(1)
    soup = BeautifulSoup(html, "html.parser")
    hidden = soup.select_one('input[name="__RequestVerificationToken"]')
    if hidden and hidden.get("value"):
        return str(hidden["value"])
    return None


def _normalize_name(name: str) -> str:
    return name.replace(" ", "").replace("　", "").strip()


def _pitcher_name_matches(probable: str, starter: str) -> bool:
    left = _normalize_name(probable)
    right = _normalize_name(starter)
    if not left or not right:
        return False
    return left in right or right in left


def first_n_runs(inning_runs: list[int], count: int = 5) -> int:
    return sum(inning_runs[:count])


def scored_innings_from_runs(runs_by_inning: list[int]) -> list[int]:
    return [index + 1 for index, runs in enumerate(runs_by_inning) if runs > 0]


def opponent_runs_by_inning(opp_innings: list[int], max_inning: int = 9) -> list[int]:
    return [(opp_innings[index] if index < len(opp_innings) else 0) for index in range(max_inning)]


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


def parse_inning_lines(scoreboard: list[dict[str, Any]]) -> tuple[list[int], list[int]]:
    away_innings = [0] * 9
    home_innings = [0] * 9
    for entry in scoreboard:
        inning = entry.get("InningSeq") or 0
        if not (1 <= inning <= 9):
            continue
        score = entry.get("ScoreCnt")
        if score is None:
            continue
        side = entry.get("VisitingHomeType")
        if side == 1:
            away_innings[inning - 1] = int(score)
        elif side == 2:
            home_innings[inning - 1] = int(score)
    return away_innings, home_innings


def _position_abbr(code: Any) -> str:
    if code is None:
        return ""
    text = str(code).strip()
    abbr = POSITION_BY_CODE.get(text, text)
    return POSITION_ZH_SHORT.get(abbr, abbr)


def _position_label(code: Any, *, hand: str | None = None) -> str:
    label = _position_abbr(code)
    if hand:
        return f"{label} ({hand})"
    return label


def _format_batting_avg(hits: int, at_bats: int) -> str | None:
    if at_bats <= 0:
        return None
    return f"{hits / at_bats:.3f}"[1:]


def _game_result(team_score: int | None, opponent_score: int | None) -> bool | None:
    if team_score is None or opponent_score is None:
        return None
    if team_score > opponent_score:
        return True
    if team_score < opponent_score:
        return False
    return None


def _parse_game_date(raw: str | None) -> str:
    if not raw:
        return ""
    return raw[:10]


def _flag_is_on(value: Any) -> bool:
    return str(value).strip() in {"1", "true", "True", "Y", "y"}


def _normalize_schedule_game(raw: dict[str, Any]) -> dict[str, Any] | None:
    away_team = team_by_code(raw.get("VisitingTeamCode"))
    home_team = team_by_code(raw.get("HomeTeamCode"))
    if not away_team or not home_team:
        return None

    iso_date = _parse_game_date(raw.get("GameDate") or raw.get("PreExeDate"))
    year = raw.get("Year")
    if not year and iso_date:
        year = int(iso_date[:4])

    if _flag_is_on(raw.get("IsGameStop")):
        status = "Cancelled"
    elif iso_date and iso_date > date.today().isoformat():
        status = "Scheduled"
    elif str(raw.get("PresentStatus")) == "1":
        status = "Final"
    else:
        status = "Scheduled"

    away_pitcher = (raw.get("VisitingPitcherName") or "").strip() or None
    home_pitcher = (raw.get("HomePitcherName") or "").strip() or None

    return {
        "gameSno": raw.get("GameSno"),
        "year": year or date.today().year,
        "date": iso_date,
        "awayTeamId": away_team["id"],
        "homeTeamId": home_team["id"],
        "awayNameZh": away_team["nameZh"],
        "homeNameZh": home_team["nameZh"],
        "awayScore": raw.get("VisitingScore"),
        "homeScore": raw.get("HomeScore"),
        "status": status,
        "stadium": raw.get("FieldAbbe") or "",
        "awayProbablePitcher": away_pitcher,
        "homeProbablePitcher": home_pitcher,
    }


def _starter_from_pitching(
    pitching: list[dict[str, Any]], *, visiting_home_type: int
) -> str | None:
    for row in pitching:
        if row.get("VisitingHomeType") != visiting_home_type:
            continue
        role = (row.get("RoleType") or "").strip()
        if role == "先發":
            name = (row.get("PitcherName") or "").strip()
            if name:
                return name
    return None


def _defensive_half_type(is_home: bool) -> int:
    # Away bats top half (1); home bats bottom half (2).
    return 1 if is_home else 2


def _opponent_score_from_entry(entry: dict[str, Any], defend_half: int) -> int | None:
    if defend_half == 1:
        score = entry.get("VisitingScore")
    else:
        score = entry.get("HomeScore")
    if score is None:
        return None
    try:
        return int(score)
    except (TypeError, ValueError):
        return None


def _parse_pitcher_runs_from_live_log(
    live_log: list[dict[str, Any]],
    is_home: bool,
    pitcher_name: str,
    opp_innings: list[int] | None = None,
) -> list[int]:
    """Runs allowed by the starter in each inning (index 0 = inning 1)."""
    if not live_log:
        return opponent_runs_by_inning(opp_innings or [])

    defend_half = _defensive_half_type(is_home)
    runs = [0] * 9
    opp = opponent_runs_by_inning(opp_innings or [])

    for inning in range(1, 10):
        half_entries = [
            entry
            for entry in live_log
            if entry.get("InningSeq") == inning and entry.get("VisitingHomeType") == defend_half
        ]
        if not half_entries:
            continue

        starter_active = False
        had_relief = False
        baseline: int | None = None
        last_score: int | None = None

        for entry in half_entries:
            pitcher = (entry.get("PitcherName") or "").strip()
            if pitcher:
                if _pitcher_name_matches(pitcher_name, pitcher):
                    starter_active = True
                    if baseline is None:
                        baseline = _opponent_score_from_entry(entry, defend_half)
                elif starter_active:
                    had_relief = True

            score = _opponent_score_from_entry(entry, defend_half)
            if score is not None:
                if baseline is None and starter_active:
                    baseline = score
                if starter_active and not had_relief:
                    last_score = score

            if starter_active and not had_relief:
                for key in ("RunCnt", "RbiCnt", "ScoreCnt"):
                    value = entry.get(key)
                    if value:
                        try:
                            runs[inning - 1] += int(value)
                        except (TypeError, ValueError):
                            pass

        if starter_active and not had_relief:
            if baseline is not None and last_score is not None and last_score > baseline:
                runs[inning - 1] = max(runs[inning - 1], last_score - baseline)
            elif runs[inning - 1] == 0 and opp[inning - 1] > 0:
                runs[inning - 1] = opp[inning - 1]

    return runs


def _parse_box_payload(raw: dict[str, Any], *, game_sno: int | str, year: int) -> dict[str, Any]:
    scoreboard = _parse_json_blob(raw.get("ScoreboardJson"))
    away_innings, home_innings = parse_inning_lines(scoreboard)
    pitching = _parse_json_blob(raw.get("PitchingJson"))
    batting = _parse_json_blob(raw.get("BattingJson"))
    first_sno = _parse_json_blob(raw.get("FirstSnoJson"))
    live_log = _parse_json_blob(raw.get("LiveLogJson"))

    return {
        "gameSno": game_sno,
        "year": year,
        "awayInnings": away_innings,
        "homeInnings": home_innings,
        "awayStarter": _starter_from_pitching(pitching, visiting_home_type=1),
        "homeStarter": _starter_from_pitching(pitching, visiting_home_type=2),
        "pitching": pitching,
        "batting": batting,
        "firstSno": first_sno,
        "liveLog": live_log,
        "scoreboard": scoreboard,
    }


def _parse_lineup_from_first_sno(
    first_sno: list[dict[str, Any]], *, visiting_home_type: int
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in first_sno
        if row.get("VisitingHomeType") == visiting_home_type and row.get("Lineup")
    ]
    rows.sort(key=lambda row: int(row.get("Lineup") or 99))
    batters: list[dict[str, Any]] = []
    for row in rows[:9]:
        order = row.get("Lineup")
        batters.append(
            {
                "order": int(order) if order is not None else len(batters) + 1,
                "id": row.get("Acnt"),
                "name": row.get("CHName") or "",
                "position": _position_abbr(row.get("DefendStationCode")),
                "positionCode": row.get("DefendStationCode"),
            }
        )
    return batters


def _team_batting_rows(
    batting: list[dict[str, Any]], *, visiting_home_type: int
) -> list[dict[str, Any]]:
    return [
        row
        for row in batting
        if row.get("VisitingHomeType") == visiting_home_type
        and (row.get("RoleType") or "").strip() in {"", "先發", "替補", "代打", "代跑"}
    ]


def _recent_batting_form(game_rows: list[dict[str, Any]]) -> dict[str, Any]:
    recent3 = game_rows[:3]
    recent5 = game_rows[:5]

    def totals(games: list[dict[str, Any]]) -> tuple[int, int, int]:
        hits = 0
        at_bats = 0
        hit_games = 0
        for game in games:
            game_hits = int(game.get("hits") or 0)
            game_ab = int(game.get("atBats") or 0)
            hits += game_hits
            at_bats += game_ab
            if game_hits > 0:
                hit_games += 1
        return hit_games, hits, at_bats

    hit_games_3, hits_3, ab_3 = totals(recent3)
    _, hits_5, ab_5 = totals(recent5)
    return {
        "recent3HitGames": hit_games_3,
        "recent3Games": len(recent3),
        "recent3Avg": _format_batting_avg(hits_3, ab_3),
        "recent5Avg": _format_batting_avg(hits_5, ab_5),
    }


def _season_avg_from_logs(game_rows: list[dict[str, Any]]) -> str | None:
    hits = sum(int(row.get("hits") or 0) for row in game_rows)
    at_bats = sum(int(row.get("atBats") or 0) for row in game_rows)
    return _format_batting_avg(hits, at_bats)


def _format_season_avg(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:.3f}"[1:]


def _collect_batter_game_logs(
    boxes: list[dict[str, Any]], team_id: int
) -> dict[str, list[dict[str, Any]]]:
    logs: dict[str, list[dict[str, Any]]] = {}
    for box in boxes:
        if not box:
            continue
        away_id = box.get("awayTeamId")
        home_id = box.get("homeTeamId")
        if team_id == away_id:
            side_type = 1
        elif team_id == home_id:
            side_type = 2
        else:
            continue

        for row in _team_batting_rows(box.get("batting") or [], visiting_home_type=side_type):
            acnt = str(row.get("HitterAcnt") or row.get("Acnt") or "")
            if not acnt:
                continue
            logs.setdefault(acnt, []).append(
                {
                    "hits": int(row.get("HitCnt") or 0),
                    "atBats": int(row.get("PlateAppearances") or 0),
                    "seasonAvg": row.get("SeasonAvg"),
                    "homeRuns": int(row.get("HomeRunCnt") or 0),
                    "rbi": int(row.get("RunBattedInCnt") or 0),
                }
            )
    return logs


def _season_avg_from_box_batting(
    batting: list[dict[str, Any]], *, visiting_home_type: int
) -> dict[str, float]:
    lookup: dict[str, float] = {}
    for row in _team_batting_rows(batting, visiting_home_type=visiting_home_type):
        acnt = str(row.get("HitterAcnt") or row.get("Acnt") or "")
        season_avg = row.get("SeasonAvg")
        if acnt and season_avg is not None:
            try:
                lookup[acnt] = float(season_avg)
            except (TypeError, ValueError):
                pass
    return lookup


def _season_row_trustworthy(season: dict[str, Any]) -> bool:
    try:
        return int(season.get("atBats") or 0) >= 15
    except (TypeError, ValueError):
        return False


def _enrich_batters_with_season_stats(
    batters: list[dict[str, Any]],
    *,
    season_lookup: dict[str, dict[str, Any]],
    box_avg_by_acnt: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for batter in batters:
        copy = dict(batter)
        acnt = str(copy.get("id") or "")
        season = season_lookup.get(acnt) or {}
        trustworthy = _season_row_trustworthy(season)

        if box_avg_by_acnt and acnt in box_avg_by_acnt:
            avg_text = format_avg(box_avg_by_acnt[acnt])
            if avg_text:
                copy["avg"] = avg_text

        if trustworthy:
            at_bats = season.get("atBats")
            hits = season.get("hits")
            if at_bats is not None and hits is not None:
                copy["atBats"] = int(at_bats)
                copy["hits"] = int(hits)
                copy["abHits"] = f"{int(at_bats)}-{int(hits)}"

            if not copy.get("avg"):
                avg_text = format_avg(season.get("avg"))
                if avg_text:
                    copy["avg"] = avg_text

            ops_text = format_ops(season.get("ops"))
            if ops_text:
                copy["ops"] = ops_text

            if season.get("homeRuns") is not None:
                copy["homeRuns"] = int(season["homeRuns"])
            if season.get("rbi") is not None:
                copy["rbi"] = int(season["rbi"])
        elif not copy.get("avg") and box_avg_by_acnt and acnt in box_avg_by_acnt:
            avg_text = format_avg(box_avg_by_acnt[acnt])
            if avg_text:
                copy["avg"] = avg_text

        hand = copy.get("hand")
        copy["positionLabel"] = _position_label(
            copy.get("positionCode") or copy.get("position"), hand=hand
        )
        enriched.append(copy)
    return enriched


def _enrich_batters_with_recent_form(
    batters: list[dict[str, Any]], logs_by_acnt: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for batter in batters:
        copy = dict(batter)
        player_logs = logs_by_acnt.get(str(copy.get("id") or ""), [])
        if player_logs:
            copy.update(_recent_batting_form(player_logs))
        enriched.append(copy)
    return enriched


def _enrich_batters_with_risp(
    batters: list[dict[str, Any]], *, players: dict[str, Any]
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for batter in batters:
        copy = dict(batter)
        acnt = str(copy.get("id") or "")
        if acnt:
            risp_avg = lookup_risp_avg(players, acnt)
            if risp_avg:
                copy["rispAvg"] = risp_avg
        enriched.append(copy)
    return enriched


def _enrich_batters_with_vs_pitcher(
    batters: list[dict[str, Any]],
    *,
    pairs: dict[str, Any],
    pitcher_acnt: str | None,
) -> list[dict[str, Any]]:
    if not pitcher_acnt:
        return batters
    enriched: list[dict[str, Any]] = []
    for batter in batters:
        copy = dict(batter)
        hitter_acnt = str(copy.get("id") or "")
        if hitter_acnt:
            copy.update(
                lookup_vs_pitcher_avgs(
                    pairs,
                    hitter_acnt=hitter_acnt,
                    pitcher_acnt=pitcher_acnt,
                )
            )
        enriched.append(copy)
    return enriched


def _resolve_opposing_pitcher(
    matchup: dict[str, Any], *, side_key: str
) -> dict[str, Any] | None:
    if side_key == "away":
        return matchup.get("home", {}).get("probablePitcher")
    return matchup.get("away", {}).get("probablePitcher")


async def _pitcher_acnt_map_for_box(
    client: CpblClient, box: dict[str, Any] | None
) -> dict[str, str]:
    if not box:
        return {}
    cached = box.get("pitcherAcntMap")
    if isinstance(cached, dict) and cached:
        return cached
    game_sno = box.get("gameSno")
    year = int(box.get("year") or date.today().year)
    if game_sno is None:
        return {}
    from app.cpbl_stats import _fetch_stats_game_html

    html = await _fetch_stats_game_html(client._http, int(game_sno), year)
    if not html:
        return {}
    mapping = parse_pitcher_acnt_map(html, game_sno=int(game_sno), year=year)
    box["pitcherAcntMap"] = mapping
    return mapping


def _load_schedule_disk() -> list[dict[str, Any]] | None:
    try:
        raw = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(raw["updatedAt"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated > SCHEDULE_DISK_TTL:
            return None
        games = raw.get("games")
        if isinstance(games, list) and len(games) >= 80:
            return games
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None


def invalidate_shared_schedule_cache() -> None:
    global _shared_schedule, _shared_box_cache
    _shared_schedule = None
    _shared_box_cache.clear()
    try:
        SCHEDULE_CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        raw = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(raw["updatedAt"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated > SCHEDULE_DISK_TTL:
            return None
        games = raw.get("games")
        if isinstance(games, list) and len(games) >= 80:
            return games
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None


def _save_schedule_disk(games: list[dict[str, Any]]) -> None:
    try:
        SCHEDULE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULE_CACHE_FILE.write_text(
            json.dumps(
                {"updatedAt": datetime.now(timezone.utc).isoformat(), "games": games},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to write CPBL schedule cache to disk")


class CpblClient:
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers=self.DEFAULT_HEADERS,
            follow_redirects=True,
        )
        self._page_tokens: dict[str, str] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _fetch_page(self, url: str) -> httpx.Response:
        response = await self._http.get(url)
        response.raise_for_status()
        if "/schedule" in str(response.url) or "/box/" in str(response.url):
            token = _extract_csrf(response.text)
            if token:
                self._page_tokens[str(response.url)] = token
        return response

    async def _post_with_csrf(
        self, page_url: str, api_path: str, data: dict[str, Any], *, page: httpx.Response | None = None
    ) -> dict[str, Any]:
        if page is None:
            page = await self._fetch_page(page_url)
        token = self._page_tokens.get(str(page.url)) or _extract_csrf(page.text)
        if not token:
            raise ValueError(f"No CSRF token found on {page_url}")

        origin = f"{page.url.scheme}://{page.url.host}"
        response = await self._http.post(
            origin + api_path,
            data=data,
            headers={
                "RequestVerificationToken": token,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": str(page.url),
            },
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"CPBL POST {api_path} returned {response.status_code}",
                request=response.request,
                response=response,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected CPBL response type from {api_path}")
        return payload

    async def fetch_schedule_pool(self, months_back: int = 6) -> list[dict[str, Any]]:
        global _shared_schedule
        async with _schedule_lock:
            if _shared_schedule is None:
                disk_games = _load_schedule_disk()
                if disk_games:
                    _shared_schedule = disk_games
                    logger.info("Loaded CPBL schedule from disk (%s games)", len(disk_games))
                else:
                    _shared_schedule = await self._build_schedule_pool(months_back)
                    _save_schedule_disk(_shared_schedule)
                    logger.info("Built CPBL schedule pool (%s games)", len(_shared_schedule))

        games = _shared_schedule
        try:
            from app.cpbl_official import enrich_schedule_probable_pitchers

            await enrich_schedule_probable_pitchers(self._http, games)
        except httpx.HTTPError:
            pass

        try:
            from app.cpbl_probable import enrich_schedule_from_ptt

            await enrich_schedule_from_ptt(self._http, games)
        except httpx.HTTPError:
            pass

        return games

    async def _build_schedule_pool(self, months_back: int = 6) -> list[dict[str, Any]]:
        today = date.today()
        month_pairs: set[tuple[int, int]] = set()

        def add_month(y: int, m: int) -> None:
            month_pairs.add((y, m))

        year = today.year
        month = today.month
        for offset in range(-(months_back + 1), 3):
            y, m = year, month + offset
            while m <= 0:
                m += 12
                y -= 1
            while m > 12:
                m -= 12
                y += 1
            add_month(y, m)

        games: list[dict[str, Any]] = []
        seen_snos: set[Any] = set()
        schedule_page: httpx.Response | None = None

        async def ingest(payload: dict[str, Any]) -> None:
            if not payload.get("Success"):
                return
            for raw in _parse_json_blob(payload.get("GameDatas")):
                normalized = _normalize_schedule_game(raw)
                if not normalized:
                    continue
                game_sno = normalized.get("gameSno")
                if game_sno in seen_snos:
                    continue
                seen_snos.add(game_sno)
                games.append(normalized)

        try:
            schedule_page = await self._fetch_page(SCHEDULE_PAGE)
            calendar_payload = await self._post_with_csrf(
                SCHEDULE_PAGE,
                "/schedule/getgamedatas",
                {"calendar": f"{today.year}/01/01", "location": "", "kindCode": KIND_CODE},
                page=schedule_page,
            )
            await ingest(calendar_payload)
        except httpx.HTTPError:
            pass

        for year_value, month_value in sorted(month_pairs):
            try:
                payload = await self._post_with_csrf(
                    SCHEDULE_PAGE,
                    "/schedule/getgamedatas",
                    {"year": year_value, "month": month_value, "kindCode": KIND_CODE},
                    page=schedule_page,
                )
            except httpx.HTTPError:
                try:
                    schedule_page = await self._fetch_page(SCHEDULE_PAGE)
                    payload = await self._post_with_csrf(
                        SCHEDULE_PAGE,
                        "/schedule/getgamedatas",
                        {"year": year_value, "month": month_value, "kindCode": KIND_CODE},
                        page=schedule_page,
                    )
                except httpx.HTTPError:
                    continue
            if not payload.get("Success"):
                try:
                    payload = await self._post_with_csrf(
                        SCHEDULE_PAGE,
                        "/schedule/getgamedatas",
                        {
                            "calendar": f"{year_value}/{month_value:02d}/01",
                            "location": "",
                            "kindCode": KIND_CODE,
                        },
                        page=schedule_page,
                    )
                except httpx.HTTPError:
                    continue
            await ingest(payload)
            await asyncio.sleep(0.15)

        if not games:
            try:
                games = await fetch_stats_schedule(self._http)
            except httpx.HTTPError:
                pass

        games.sort(key=lambda game: (game.get("date", ""), game.get("gameSno") or 0))
        return games

    async def fetch_box(self, game_sno: int | str, year: int) -> dict[str, Any] | None:
        global _shared_box_cache
        cache_key = f"{year}:{game_sno}"
        if cache_key in _shared_box_cache:
            return _shared_box_cache[cache_key]

        page_url = f"{CPBL_BASE}/box/index?gameSno={game_sno}&kindCode={KIND_CODE}"
        try:
            payload = await self._post_with_csrf(
                page_url,
                "/box/getlive",
                {"gameSno": str(game_sno), "kindCode": KIND_CODE, "year": year},
            )
        except (httpx.HTTPError, ValueError):
            payload = None

        parsed: dict[str, Any] | None = None
        if payload and payload.get("Success"):
            parsed = _parse_box_payload(payload, game_sno=game_sno, year=year)
        else:
            try:
                parsed = await fetch_stats_box(self._http, game_sno, year)
            except httpx.HTTPError:
                parsed = None

        if not parsed:
            return None
        schedule = await self.fetch_schedule_pool()
        for game in schedule:
            if game.get("gameSno") == game_sno:
                parsed["awayTeamId"] = game["awayTeamId"]
                parsed["homeTeamId"] = game["homeTeamId"]
                parsed["date"] = game.get("date")
                break

        _shared_box_cache[cache_key] = parsed
        return parsed


async def fetch_cpbl_teams() -> list[dict[str, Any]]:
    return list_teams()


async def _fetch_boxes_limited(
    client: CpblClient, metas: list[dict[str, Any]]
) -> list[dict[str, Any] | None]:
    async def fetch_one(meta: dict[str, Any]) -> dict[str, Any] | None:
        async with _FETCH_SEM:
            game_sno = meta.get("gameSno")
            year = meta.get("year") or date.today().year
            if game_sno is None:
                return None
            box = await client.fetch_box(game_sno, int(year))
            if box:
                box.setdefault("awayTeamId", meta.get("awayTeamId"))
                box.setdefault("homeTeamId", meta.get("homeTeamId"))
                box.setdefault("date", meta.get("date"))
            return box

    return list(await asyncio.gather(*[fetch_one(meta) for meta in metas]))


async def fetch_next_matchup(client: CpblClient, focus_team_id: int) -> dict[str, Any] | None:
    schedule = await client.fetch_schedule_pool()
    today = datetime.now(TPE).date().isoformat()

    team_games = [
        game
        for game in schedule
        if focus_team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game["status"] not in {"Final", "Cancelled"}
    ]
    if not team_games:
        return None

    future = [game for game in team_games if game.get("date", "") >= today]
    pool = future if future else team_games

    def sort_key(game: dict[str, Any]) -> tuple[str, int, Any]:
        game_date = game.get("date") or "9999-99-99"
        has_pitchers = int(
            not (game.get("awayProbablePitcher") and game.get("homeProbablePitcher"))
        )
        return (game_date, has_pitchers, game.get("gameSno") or 0)

    pool.sort(key=sort_key)
    game = pool[0]
    focus_is_home = game["homeTeamId"] == focus_team_id

    def side_info(team_id: int, probable: str | None) -> dict[str, Any]:
        return {
            "teamId": team_id,
            "teamName": team_zh(team_id),
            "probablePitcher": {"fullName": probable} if probable else None,
        }

    return {
        "date": game.get("date"),
        "gameDate": f"{game.get('date')}T18:00:00+08:00",
        "gameSno": game.get("gameSno"),
        "year": game.get("year"),
        "status": game.get("status"),
        "stadium": game.get("stadium"),
        "focusTeamId": focus_team_id,
        "away": side_info(game["awayTeamId"], game.get("awayProbablePitcher")),
        "home": side_info(game["homeTeamId"], game.get("homeProbablePitcher")),
        "focusIsHome": focus_is_home,
    }


def _build_team_scoring_row(
    meta: dict[str, Any],
    box: dict[str, Any],
    team_id: int,
    *,
    include_scored: bool = False,
) -> dict[str, Any]:
    is_home = box.get("homeTeamId") == team_id
    side = "home" if is_home else "away"
    opp_side = "away" if is_home else "home"
    inning_runs = box[f"{side}Innings"]
    first_inning = inning_runs[0] if inning_runs else 0
    first_five = first_n_runs(inning_runs, 5)
    opponent_id = box.get(f"{opp_side}TeamId")
    if opponent_id is None:
        opponent_id = meta["homeTeamId"] if not is_home else meta["awayTeamId"]
    opponent_starter = box.get(f"{opp_side}Starter")

    team_score = meta.get("homeScore" if is_home else "awayScore")
    opponent_score = meta.get("awayScore" if is_home else "homeScore")
    if team_score is None:
        team_score = box.get("homeScore" if is_home else "awayScore")
    if opponent_score is None:
        opponent_score = box.get("awayScore" if is_home else "homeScore")
    if team_score is None or opponent_score is None:
        team_score = sum(box[f"{side}Innings"])
        opponent_score = sum(box[f"{opp_side}Innings"])

    row: dict[str, Any] = {
        "date": meta.get("date"),
        "gameSno": meta.get("gameSno"),
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
        "result": _game_result(team_score, opponent_score),
    }
    if include_scored:
        scored_innings: list[int] = []
        for index in range(9):
            runs = inning_runs[index] if index < len(inning_runs) else 0
            if runs > 0:
                scored_innings.append(index + 1)
        row["scoredInnings"] = scored_innings
    return row


def _row_has_scoring_data(row: dict[str, Any]) -> bool:
    if row.get("firstFiveRuns", 0) > 0:
        return True
    team_score = row.get("teamScore")
    opponent_score = row.get("opponentScore")
    if team_score is None or opponent_score is None:
        return False
    return int(team_score) + int(opponent_score) > 0


def _box_has_inning_data(box: dict[str, Any]) -> bool:
    away = box.get("awayInnings") or []
    home = box.get("homeInnings") or []
    return sum(away) + sum(home) > 0


async def _build_location_scored_pool(
    client: CpblClient,
    team_id: int,
    *,
    is_home: bool,
    limit: int = 10,
) -> list[dict[str, Any]]:
    schedule = await client.fetch_schedule_pool()
    finals = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("gameSno") is not None
        and (game["homeTeamId"] == team_id if is_home else game["awayTeamId"] == team_id)
    ]
    finals.sort(key=lambda game: game.get("date", ""), reverse=True)

    candidates = finals[: max(limit * 3, limit)]
    parsed_list = await _fetch_boxes_limited(client, candidates)
    rows: list[dict[str, Any]] = []
    for meta, box in zip(candidates, parsed_list):
        if len(rows) >= limit:
            break
        if not box or not _box_has_inning_data(box):
            continue
        rows.append(_build_team_scoring_row(meta, box, team_id, include_scored=True))
    return rows


async def analyze_team_scoring(
    client: CpblClient, team_id: int, game_count: int = 10
) -> dict[str, Any]:
    team = TEAM_BY_ID[team_id]
    schedule = await client.fetch_schedule_pool()
    pool = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("gameSno") is not None
    ]
    pool.sort(key=lambda game: game.get("date", ""), reverse=True)
    panel_meta = pool[: max(game_count, 20)]

    if not pool:
        return {
            "teamId": team_id,
            "teamName": team["nameZh"],
            "games": [],
            "_scoredPool": [],
            "summary": summarize_team_scoring([], []),
        }

    away_pool, home_pool = await asyncio.gather(
        _build_location_scored_pool(client, team_id, is_home=False, limit=10),
        _build_location_scored_pool(client, team_id, is_home=True, limit=10),
    )
    scored_pool = away_pool + home_pool

    needed_meta: list[dict[str, Any]] = []
    seen_snos: set[Any] = set()
    for meta in panel_meta:
        game_sno = meta.get("gameSno")
        if game_sno in seen_snos:
            continue
        seen_snos.add(game_sno)
        needed_meta.append(meta)

    parsed_list = await _fetch_boxes_limited(client, needed_meta)
    rows: list[dict[str, Any]] = []
    for meta, box in zip(needed_meta, parsed_list):
        if not box or not _box_has_inning_data(box):
            continue
        row = _build_team_scoring_row(meta, box, team_id)
        if _row_has_scoring_data(row):
            rows.append(row)
        if len(rows) >= game_count:
            break

    runs_list = [row["firstFiveRuns"] for row in rows]
    return {
        "teamId": team_id,
        "teamName": team["nameZh"],
        "games": rows[:game_count],
        "_scoredPool": scored_pool,
        "summary": summarize_team_scoring(rows[:game_count], runs_list),
    }


def _starter_pitching_line(
    box: dict[str, Any], team_id: int, pitcher_name: str
) -> dict[str, Any] | None:
    is_home = box.get("homeTeamId") == team_id
    side_type = 2 if is_home else 1
    for row in box.get("pitching") or []:
        if row.get("VisitingHomeType") != side_type:
            continue
        if (row.get("RoleType") or "").strip() != "先發":
            continue
        if _pitcher_name_matches(pitcher_name, row.get("PitcherName") or ""):
            return row
    return None


def _fallback_pitcher_runs_by_inning(
    box: dict[str, Any],
    team_id: int,
    pitcher_name: str,
    opp_innings: list[int],
) -> list[int]:
    """Estimate per-inning runs allowed without play-by-play."""
    line = _starter_pitching_line(box, team_id, pitcher_name)
    opp = opponent_runs_by_inning(opp_innings)
    runs = [0] * 9
    if not line:
        return runs

    try:
        max_inning = min(9, max(1, int(line.get("InningPitchedCnt") or 9)))
    except (TypeError, ValueError):
        max_inning = 9

    official_runs = line.get("EarnedRunCnt")
    if official_runs is None:
        official_runs = line.get("RunCnt")
    try:
        official_total = int(official_runs) if official_runs is not None else sum(opp)
    except (TypeError, ValueError):
        official_total = sum(opp)

    remaining = official_total
    for index in list(range(min(max_inning, 9))) + list(range(max_inning, 9)):
        if remaining <= 0:
            break
        if opp[index] <= 0:
            continue
        take = min(int(opp[index]), remaining)
        runs[index] = take
        remaining -= take
    return runs


def _pitcher_runs_with_starter_innings(
    box: dict[str, Any],
    team_id: int,
    pitcher_name: str,
    runs_by_inning: list[int],
    *,
    used_live_log: bool,
    opp_innings: list[int],
) -> list[int]:
    if used_live_log:
        return runs_by_inning
    return _fallback_pitcher_runs_by_inning(box, team_id, pitcher_name, opp_innings)


def _pitcher_row_from_box(
    meta: dict[str, Any],
    box: dict[str, Any],
    team_id: int,
    pitcher_name: str,
) -> dict[str, Any]:
    is_home = box.get("homeTeamId") == team_id
    side = "home" if is_home else "away"
    opp_side = "away" if is_home else "home"
    opp_innings = box["awayInnings" if is_home else "homeInnings"]
    live_log = box.get("liveLog") or []
    used_live_log = bool(live_log)

    runs_by_inning = _parse_pitcher_runs_from_live_log(
        live_log, is_home, pitcher_name, opp_innings
    )
    runs_by_inning = _pitcher_runs_with_starter_innings(
        box,
        team_id,
        pitcher_name,
        runs_by_inning,
        used_live_log=used_live_log,
        opp_innings=opp_innings,
    )
    first_five_allowed = first_n_runs(runs_by_inning, 5)
    first_inning_allowed = runs_by_inning[0] if runs_by_inning else 0

    opponent_id = box.get(f"{opp_side}TeamId")
    if opponent_id is None:
        opponent_id = meta["homeTeamId"] if not is_home else meta["awayTeamId"]

    team_score = meta.get("homeScore" if is_home else "awayScore")
    opponent_score = meta.get("awayScore" if is_home else "homeScore")
    if team_score is None or opponent_score is None:
        team_score = sum(box[f"{side}Innings"])
        opponent_score = sum(box[f"{opp_side}Innings"])

    line = _starter_pitching_line(box, team_id, pitcher_name)
    innings_pitched = line.get("InningPitchedCnt") if line else None
    earned_runs = None
    if line:
        earned_runs = line.get("EarnedRunCnt")
        if earned_runs is None:
            earned_runs = line.get("RunCnt")

    return {
        "date": meta.get("date"),
        "gameSno": meta.get("gameSno"),
        "opponent": team_zh(opponent_id),
        "opponentStarter": box.get(f"{opp_side}Starter"),
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
        "inningsPitched": innings_pitched,
        "earnedRuns": earned_runs,
        "result": _game_result(team_score, opponent_score),
    }


async def analyze_pitcher_starts(
    client: CpblClient,
    pitcher_name: str,
    team_id: int,
    game_count: int = 10,
    *,
    scan_limit: int = 40,
) -> dict[str, Any]:
    if not pitcher_name:
        empty = summarize_pitcher_summary([], [])
        return {"pitcherName": pitcher_name, "games": [], "summary": empty}

    schedule = await client.fetch_schedule_pool()
    candidates = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("gameSno") is not None
    ]
    candidates.sort(key=lambda game: game.get("date", ""), reverse=True)

    rows: list[dict[str, Any]] = []
    batch_size = 15
    for index in range(0, len(candidates), batch_size):
        if len(rows) >= scan_limit:
            break
        batch = candidates[index : index + batch_size]
        boxes = await _fetch_boxes_limited(client, batch)
        for meta, box in zip(batch, boxes):
            if not box:
                continue
            is_home = box.get("homeTeamId") == team_id
            side = "home" if is_home else "away"
            starter = box.get(f"{side}Starter")
            if not starter or not _pitcher_name_matches(pitcher_name, starter):
                continue
            rows.append(_pitcher_row_from_box(meta, box, team_id, pitcher_name))

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
    client: CpblClient, team_id: int, *, game_count: int = 20
) -> dict[str, Any]:
    team = TEAM_BY_ID[team_id]
    schedule = await client.fetch_schedule_pool()
    finished = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("gameSno") is not None
    ]
    finished.sort(key=lambda game: game.get("date", ""), reverse=True)
    finished = finished[:game_count]

    rows: list[dict[str, Any]] = []
    if finished:
        for meta in finished:
            if len(rows) >= game_count:
                break
            box = await client.fetch_box(meta["gameSno"], int(meta.get("year") or date.today().year))
            if not box or not _box_has_inning_data(box):
                continue
            is_home = box.get("homeTeamId") == team_id
            side = "home" if is_home else "away"
            opp_side = "away" if is_home else "home"
            my_innings = box[f"{side}Innings"]
            opp_innings = box[f"{opp_side}Innings"]

            scored_innings: list[int] = []
            allowed_innings: list[int] = []
            for inning_index in range(9):
                runs = my_innings[inning_index] if inning_index < len(my_innings) else 0
                runs_allowed = opp_innings[inning_index] if inning_index < len(opp_innings) else 0
                inning = inning_index + 1
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


async def _recent_team_boxes(
    client: CpblClient, team_id: int, *, limit: int = 5
) -> list[dict[str, Any]]:
    schedule = await client.fetch_schedule_pool()
    finished = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("gameSno") is not None
    ]
    finished.sort(key=lambda game: game.get("date", ""), reverse=True)
    finished = finished[:limit]
    boxes = await _fetch_boxes_limited(client, finished)
    results: list[dict[str, Any]] = []
    for meta, box in zip(finished, boxes):
        if not box:
            continue
        copy = dict(box)
        copy.setdefault("awayTeamId", meta.get("awayTeamId"))
        copy.setdefault("homeTeamId", meta.get("homeTeamId"))
        copy.setdefault("date", meta.get("date"))
        results.append(copy)
    return results


async def _lineup_for_team(
    client: CpblClient,
    team_id: int,
    *,
    preferred_box: dict[str, Any] | None = None,
    preferred_date: str | None = None,
) -> tuple[list[dict[str, Any]], str, str | None]:
    visiting_home_type = 2 if preferred_box and preferred_box.get("homeTeamId") == team_id else 1
    if preferred_box:
        batters = _parse_lineup_from_first_sno(
            preferred_box.get("firstSno") or [],
            visiting_home_type=visiting_home_type,
        )
        if batters:
            return batters, "confirmed", preferred_date

    schedule = await client.fetch_schedule_pool()
    finished = [
        game
        for game in schedule
        if game["status"] == "Final"
        and team_id in {game["awayTeamId"], game["homeTeamId"]}
        and game.get("gameSno") is not None
    ]
    finished.sort(key=lambda game: game.get("date", ""), reverse=True)
    finished = finished[:5]
    if finished:
        boxes = await _fetch_boxes_limited(client, finished)
        for meta, box in zip(finished, boxes):
            if not box:
                continue
            side_type = 2 if meta["homeTeamId"] == team_id else 1
            batters = _parse_lineup_from_first_sno(box.get("firstSno") or [], visiting_home_type=side_type)
            if batters:
                return batters, "previous", meta.get("date")

    return [], "previous", None


async def fetch_matchup_starting_lineups(
    client: CpblClient, matchup: dict[str, Any]
) -> dict[str, Any]:
    upcoming_box: dict[str, Any] | None = None
    game_sno = matchup.get("gameSno")
    year = int(matchup.get("year") or date.today().year)
    if game_sno is not None:
        upcoming_box = await client.fetch_box(game_sno, year)

    pitcher_name_map = await _pitcher_acnt_map_for_box(client, upcoming_box)
    vs_pairs = await get_vs_pitcher_lookup(year)
    if not vs_pairs:
        schedule_vs_pitcher_refresh(year)
    risp_players = await get_risp_lookup(year)
    if not risp_players:
        schedule_risp_refresh(year)

    lineups: dict[str, Any] = {}
    for side_key in ("away", "home"):
        side_info = matchup[side_key]
        team_id = side_info["teamId"]
        preferred_side_type = 1 if side_key == "away" else 2
        preferred_box = upcoming_box
        if preferred_box and side_key == "away" and preferred_box.get("awayTeamId") != team_id:
            preferred_box = None
        if preferred_box and side_key == "home" and preferred_box.get("homeTeamId") != team_id:
            preferred_box = None

        batters, source, source_date = await _lineup_for_team(
            client,
            team_id,
            preferred_box=preferred_box,
            preferred_date=matchup.get("date"),
        )
        if preferred_box and batters and source == "confirmed":
            batters = _parse_lineup_from_first_sno(
                preferred_box.get("firstSno") or [],
                visiting_home_type=preferred_side_type,
            ) or batters

        if batters:
            season_lookup = await get_season_batting_lookup(year)
            if not season_lookup:
                schedule_season_batting_refresh(year)
            box_avg_by_acnt: dict[str, float] = {}
            if preferred_box:
                for side_type in (1, 2):
                    box_avg_by_acnt.update(
                        _season_avg_from_box_batting(
                            preferred_box.get("batting") or [],
                            visiting_home_type=side_type,
                        )
                    )
            batters = _enrich_batters_with_season_stats(
                batters,
                season_lookup=season_lookup,
                box_avg_by_acnt=box_avg_by_acnt,
            )

            recent_boxes = await _recent_team_boxes(client, team_id, limit=5)
            logs_by_acnt = _collect_batter_game_logs(recent_boxes, team_id)
            batters = _enrich_batters_with_recent_form(batters, logs_by_acnt)

            opposing = _resolve_opposing_pitcher(matchup, side_key=side_key)
            pitcher_name = (opposing or {}).get("fullName") if opposing else None
            pitcher_acnt = pitcher_name_map.get((pitcher_name or "").strip()) if pitcher_name else None
            batters = _enrich_batters_with_vs_pitcher(
                batters,
                pairs=vs_pairs,
                pitcher_acnt=pitcher_acnt,
            )
            batters = _enrich_batters_with_risp(batters, players=risp_players)

        opposing = _resolve_opposing_pitcher(matchup, side_key=side_key)
        lineups[side_key] = {
            "teamName": side_info["teamName"],
            "source": source,
            "sourceDate": source_date or matchup.get("date"),
            "opposingPitcher": opposing,
            "batters": batters,
        }
    return lineups


async def _build_side_panel(
    client: CpblClient, side_info: dict[str, Any], game_count: int
) -> dict[str, Any]:
    scoring = await analyze_team_scoring(client, side_info["teamId"], game_count)
    probable = side_info.get("probablePitcher")
    pitcher_analysis = None
    if probable and probable.get("fullName"):
        pitcher_analysis = await analyze_pitcher_starts(
            client,
            probable["fullName"],
            side_info["teamId"],
            game_count,
            scan_limit=40,
        )

    return {
        **scoring,
        "probablePitcher": probable,
        "pitcherAnalysis": pitcher_analysis,
    }


async def analyze_matchup(focus_team_id: int, game_count: int = 10) -> dict[str, Any]:
    client = CpblClient()
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
        starting_lineups = {"away": {"batters": []}, "home": {"batters": []}}
        situational = build_matchup_situational(away_panel, home_panel)
        away_panel = strip_panel_internals(away_panel)
        home_panel = strip_panel_internals(home_panel)
    finally:
        await client.close()

    return {
        "focusTeamId": focus_team_id,
        "matchup": {
            "date": matchup.get("date"),
            "gameDate": matchup.get("gameDate"),
            "gameSno": matchup.get("gameSno"),
            "status": matchup.get("status"),
            "stadium": matchup.get("stadium"),
        },
        "away": away_panel,
        "home": home_panel,
        "aTable": {"away": away_table, "home": home_table},
        "startingLineups": starting_lineups,
        "situational": situational,
    }


async def analyze_matchup_a_table(focus_team_id: int) -> dict[str, Any]:
    client = CpblClient()
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
