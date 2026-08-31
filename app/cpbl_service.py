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
from app.pitcher_rows import pitch_count_from_cpbl_line
from app.cpbl_stats import fetch_stats_box, fetch_stats_schedule
from app.cpbl_teams import TEAM_BY_ID, list_teams, team_by_code, team_zh
from app.inning_comparison import (
    build_inning_comparison,
    build_matchup_situational,
    strip_panel_internals,
)

logger = logging.getLogger(__name__)


def _row_side_type(row: dict[str, Any]) -> int | None:
    """CPBL APIs often return VisitingHomeType as str '1'/'2'."""
    raw = row.get("VisitingHomeType")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# Prefer non-www origin: HiNet CDN on www.cpbl.com.tw often 307s schedule/API
# to the homepage or serves broken nodes (empty today's games / no pitchers).
CPBL_BASE = "https://cpbl.com.tw"
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


def _inning_seq(entry: dict[str, Any]) -> int | None:
    """CPBL may return InningSeq as int, float, or str."""
    raw = entry.get("InningSeq")
    if raw is None or raw == "":
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 20 else None


def parse_inning_lines(scoreboard: list[dict[str, Any]]) -> tuple[list[int], list[int]]:
    away_innings = [0] * 9
    home_innings = [0] * 9
    for entry in scoreboard:
        inning = _inning_seq(entry)
        if inning is None or inning > 9:
            continue
        score = entry.get("ScoreCnt")
        if score is None:
            continue
        side = _row_side_type(entry)
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
    text = str(raw).strip().replace("/", "-")
    return text[:10]


def _flag_is_on(value: Any) -> bool:
    return str(value).strip() in {"1", "true", "True", "Y", "y"}


def _score_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scores_present(raw: dict[str, Any]) -> bool:
    return (
        _score_value(raw.get("VisitingScore")) is not None
        and _score_value(raw.get("HomeScore")) is not None
    )


def _game_has_decision(raw: dict[str, Any]) -> bool:
    """True when the schedule row shows a completed game (W/L, end time, or MVP)."""
    for key in (
        "WinningPitcherAcnt",
        "WinningPitcherName",
        "LoserPitcherAcnt",
        "LoserPitcherName",
        "GameDateTimeE",
        "GameDuringTime",
        "MvpAcnt",
        "MvpName",
    ):
        value = raw.get(key)
        if value is None:
            continue
        if str(value).strip() not in {"", "0", "null", "None"}:
            return True
    return False


def _derive_schedule_status(raw: dict[str, Any], iso_date: str) -> str:
    """Map CPBL schedule flags to our status.

    PresentStatus=1 is unreliable: it is set for future shells and live games.
    Live rows use IsPlayBall=Y and lack WinningPitcher / GameDateTimeE until final.
    """
    if _flag_is_on(raw.get("IsGameStop")):
        return "Cancelled"
    today = date.today().isoformat()
    if iso_date and iso_date > today:
        return "Scheduled"
    if _game_has_decision(raw):
        return "Final"
    if _flag_is_on(raw.get("IsPlayBall")):
        return "In Progress"
    if iso_date == today:
        away = _score_value(raw.get("VisitingScore")) or 0
        home = _score_value(raw.get("HomeScore")) or 0
        # Non-zero score without a decision means the game is still underway.
        if _scores_present(raw) and (away != 0 or home != 0):
            return "In Progress"
        return "Scheduled"
    # Past date with scores and PresentStatus — treat as final even if decision
    # fields were stripped from an older cache rebuild path.
    if str(raw.get("PresentStatus")) == "1" and _scores_present(raw):
        return "Final"
    return "Scheduled"


def _repair_cached_game_status(game: dict[str, Any]) -> None:
    """Fix disk-cached status (esp. live games wrongly stored as Final)."""
    if game.get("status") == "Cancelled":
        return
    game_date = game.get("date") or ""
    today = date.today().isoformat()
    has_decision = bool(game.get("hasDecision"))
    is_play_ball = bool(game.get("isPlayBall"))

    if game_date > today:
        game["status"] = "Scheduled"
        return
    if has_decision:
        game["status"] = "Final"
        return

    away = _score_value(game.get("awayScore"))
    home = _score_value(game.get("homeScore"))
    scored = (
        away is not None
        and home is not None
        and (away != 0 or home != 0)
    )

    if game_date == today:
        if is_play_ball or scored:
            game["status"] = "In Progress"
        elif game.get("status") == "Final":
            # Older caches marked live/pre-game shells Final once PresentStatus=1.
            game["status"] = "Scheduled"
        return

    # Past calendar day: promote leftover In Progress → Final.
    if game.get("status") == "In Progress":
        game["status"] = "Final"
    elif game.get("status") == "Final" and (
        away is None or home is None or (away == 0 and home == 0)
    ):
        game["status"] = "Scheduled"


def _normalize_schedule_game(raw: dict[str, Any]) -> dict[str, Any] | None:
    away_team = team_by_code(raw.get("VisitingTeamCode"))
    home_team = team_by_code(raw.get("HomeTeamCode"))
    if not away_team or not home_team:
        return None

    iso_date = _parse_game_date(raw.get("GameDate") or raw.get("PreExeDate"))
    year = raw.get("Year")
    if not year and iso_date:
        year = int(iso_date[:4])

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
        "awayScore": _score_value(raw.get("VisitingScore")),
        "homeScore": _score_value(raw.get("HomeScore")),
        "status": _derive_schedule_status(raw, iso_date),
        "stadium": raw.get("FieldAbbe") or "",
        "awayProbablePitcher": away_pitcher,
        "homeProbablePitcher": home_pitcher,
        "hasDecision": _game_has_decision(raw),
        "isPlayBall": _flag_is_on(raw.get("IsPlayBall")),
    }


def _starter_from_pitching(
    pitching: list[dict[str, Any]], *, visiting_home_type: int
) -> str | None:
    for row in pitching:
        if _row_side_type(row) != visiting_home_type:
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
            if _inning_seq(entry) == inning and _row_side_type(entry) == defend_half
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
    """Official firstSno repeats the same Lineup slot when a player changes position.

    Keep the first row per batting order 1–9 (pitchers use Lineup 0).
    """
    rows = [
        row
        for row in first_sno
        if _row_side_type(row) == visiting_home_type
    ]
    by_order: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            order = int(row.get("Lineup") or 0)
        except (TypeError, ValueError):
            continue
        if order < 1 or order > 9:
            continue
        if order in by_order:
            continue
        name = (row.get("CHName") or "").strip()
        if not name:
            continue
        # Skip pure pitchers in the batting card (DefendStationCode P / Lineup noise).
        pos = str(row.get("DefendStationCode") or "").strip().upper()
        if pos == "P":
            continue
        by_order[order] = {
            "order": order,
            "id": row.get("Acnt"),
            "name": name,
            "position": _position_abbr(row.get("DefendStationCode")),
            "positionCode": row.get("DefendStationCode"),
        }
    return [by_order[order] for order in range(1, 10) if order in by_order]


def _parse_lineup_from_batting(
    batting: list[dict[str, Any]], *, visiting_home_type: int
) -> list[dict[str, Any]]:
    """Live/finished boxes often omit Lineup on batting rows; array order of 先發 is the card."""
    starters = [
        row
        for row in batting
        if _row_side_type(row) == visiting_home_type
        and (row.get("RoleType") or "").strip() == "先發"
        and (row.get("CHName") or row.get("HitterName") or "").strip()
    ]
    batters: list[dict[str, Any]] = []
    for index, row in enumerate(starters[:9], start=1):
        acnt = row.get("Acnt") or row.get("HitterAcnt")
        batters.append(
            {
                "order": index,
                "id": acnt,
                "name": (row.get("CHName") or row.get("HitterName") or "").strip(),
                "position": "",
                "positionCode": None,
            }
        )
    return batters


def _merge_lineup_positions(
    batters: list[dict[str, Any]], first_sno: list[dict[str, Any]], *, visiting_home_type: int
) -> list[dict[str, Any]]:
    by_acnt: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for row in first_sno:
        if _row_side_type(row) != visiting_home_type:
            continue
        code = row.get("DefendStationCode")
        if not code:
            continue
        entry = {
            "position": _position_abbr(code),
            "positionCode": code,
        }
        acnt = row.get("Acnt")
        name = (row.get("CHName") or "").strip()
        if acnt:
            by_acnt[str(acnt)] = entry
        if name:
            by_name[name] = entry
    merged: list[dict[str, Any]] = []
    for batter in batters:
        copy = dict(batter)
        acnt = copy.get("id")
        name = (copy.get("name") or "").strip()
        extra = (by_acnt.get(str(acnt)) if acnt else None) or by_name.get(name)
        if extra and not copy.get("position"):
            copy.update(extra)
        merged.append(copy)
    return merged


def _parse_lineup_from_box(
    box: dict[str, Any], *, visiting_home_type: int
) -> list[dict[str, Any]]:
    """Prefer complete firstSno; fall back to batting 先發 order (works while games are live)."""
    first_sno = box.get("firstSno") or []
    from_sno = _parse_lineup_from_first_sno(first_sno, visiting_home_type=visiting_home_type)
    if _lineup_is_complete(from_sno):
        return from_sno
    from_batting = _parse_lineup_from_batting(
        box.get("batting") or [], visiting_home_type=visiting_home_type
    )
    if _lineup_is_complete(from_batting):
        return _merge_lineup_positions(
            from_batting, first_sno, visiting_home_type=visiting_home_type
        )
    if len(from_sno) >= len(from_batting):
        return from_sno
    return _merge_lineup_positions(
        from_batting, first_sno, visiting_home_type=visiting_home_type
    )


def _lineup_is_complete(batters: list[dict[str, Any]]) -> bool:
    """Require unique batting orders 1–9 (len>=9 alone accepts mid-game dupes)."""
    if len(batters) != 9:
        return False
    orders = [int(b.get("order") or 0) for b in batters]
    names = [(b.get("name") or "").strip() for b in batters]
    if len(set(orders)) != 9 or set(orders) != set(range(1, 10)):
        return False
    if len({n for n in names if n}) != 9:
        return False
    return True


def _lineup_orders_unique(batters: list[dict[str, Any]]) -> bool:
    """Reject mid-game firstSno slices that repeat the same batting order."""
    if not batters:
        return True
    orders: list[int] = []
    for batter in batters:
        try:
            order = int(batter.get("order") or 0)
        except (TypeError, ValueError):
            return False
        if order < 1 or order > 9:
            return False
        orders.append(order)
    return len(orders) == len(set(orders))


# Bump when firstSno dedupe / order rules change so cached cards rebuild.
CPBL_LINEUP_LOGIC_VERSION = 3


def cpbl_lineups_need_rebuild(
    lineups: dict[str, Any] | None,
    *,
    matchup_date: str | None = None,
    matchup_status: str | None = None,
) -> bool:
    """True when lineup card is missing, stale logic, or not a clean 1–9 card."""
    if not lineups:
        return True
    if lineups.get("logicVersion") != CPBL_LINEUP_LOGIC_VERSION:
        return True
    away = (lineups.get("away") or {}).get("batters") or []
    home = (lineups.get("home") or {}).get("batters") or []
    if away and (not _lineup_orders_unique(away) or not _lineup_is_complete(away)):
        return True
    if home and (not _lineup_orders_unique(home) or not _lineup_is_complete(home)):
        return True
    if not away and not home:
        return True
    # Game day: previous-game card must be refreshed once today's lineup exists.
    if matchup_date:
        status = (matchup_status or "").strip().lower()
        for side in ("away", "home"):
            side_data = lineups.get(side) or {}
            if not (side_data.get("batters") or []):
                continue
            source_date = (side_data.get("sourceDate") or "")[:10]
            source = (side_data.get("source") or "").strip().lower()
            if source_date and source_date != matchup_date and status not in {"final"}:
                return True
            if (
                source != "confirmed"
                and source_date == matchup_date
                and status in {"scheduled", "live", "in progress", ""}
            ):
                return True
    return False


def _lookup_pitcher_acnt(name_map: dict[str, str], pitcher_name: str | None) -> str | None:
    if not pitcher_name:
        return None
    needle = pitcher_name.strip()
    if not needle:
        return None
    direct = name_map.get(needle)
    if direct:
        return direct
    for name, acnt in name_map.items():
        if _pitcher_name_matches(needle, name):
            return acnt
    return None


def _pitcher_acnt_cache_file() -> Path:
    return BASE_DIR / "data" / "cpbl_pitcher_acnt.json"


def _load_pitcher_acnt_disk() -> dict[str, str]:
    path = _pitcher_acnt_cache_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        names = raw.get("names")
        if isinstance(names, dict):
            return {str(k): str(v) for k, v in names.items() if k and v}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def _save_pitcher_acnt_disk(mapping: dict[str, str]) -> None:
    path = _pitcher_acnt_cache_file()
    existing = _load_pitcher_acnt_disk()
    existing.update({k: v for k, v in mapping.items() if k and v})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                    "names": existing,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("Failed to save pitcher Acnt cache")


async def _build_pitcher_name_acnt_map(
    client: CpblClient,
    *,
    year: int,
    seed_map: dict[str, str] | None = None,
    needed_names: list[str] | None = None,
) -> dict[str, str]:
    """Resolve pitcher Chinese name → Acnt from disk cache + recent PBP maps."""
    mapping: dict[str, str] = {}
    mapping.update(_load_pitcher_acnt_disk())
    mapping.update(seed_map or {})
    needed = [n.strip() for n in (needed_names or []) if n and str(n).strip()]

    def missing() -> list[str]:
        return [n for n in needed if not _lookup_pitcher_acnt(mapping, n)]

    if needed and not missing():
        return mapping

    schedule = await client.fetch_schedule_pool()
    finished = [
        game
        for game in schedule
        if game.get("status") == "Final" and game.get("gameSno") is not None
    ]
    finished.sort(key=lambda game: game.get("date", ""), reverse=True)

    html_fetches = 0
    for meta in finished[:40]:
        still_missing = missing()
        if needed and not still_missing:
            break
        box = await client.fetch_box(int(meta["gameSno"]), int(meta.get("year") or year))
        if not box:
            continue
        starters = [
            (box.get("awayStarter") or "").strip(),
            (box.get("homeStarter") or "").strip(),
        ]
        if still_missing and not any(
            any(_pitcher_name_matches(need, starter) for need in still_missing)
            for starter in starters
            if starter
        ):
            continue
        if html_fetches >= 6:
            break
        try:
            partial = await _pitcher_acnt_map_for_box(client, box)
            html_fetches += 1
        except Exception:
            continue
        if partial:
            mapping.update(partial)

    if mapping:
        _save_pitcher_acnt_disk(mapping)
    return mapping


def _team_batting_rows(
    batting: list[dict[str, Any]], *, visiting_home_type: int
) -> list[dict[str, Any]]:
    return [
        row
        for row in batting
        if _row_side_type(row) == visiting_home_type
        and (row.get("RoleType") or "").strip() in {"", "先發", "替補", "代打", "代跑"}
    ]


def _box_row_hits(row: dict[str, Any]) -> int:
    """Hits from an official or stats-normalized batting row.

    Official API: HittingCnt = hits, HitCnt = at-bats (misnamed).
    stats.cpbl normalized rows: HitCnt = hits, no HittingCnt; hit-type counts present.
    """
    typed = sum(
        int(row.get(key) or 0)
        for key in (
            "OneBaseHitCnt",
            "TwoBaseHitCnt",
            "ThreeBaseHitCnt",
            "HomeRunCnt",
        )
    )
    if typed:
        return typed
    if row.get("HittingCnt") is not None:
        try:
            return int(row.get("HittingCnt") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(row.get("HitCnt") or 0)
    except (TypeError, ValueError):
        return 0


def _box_row_at_bats(row: dict[str, Any]) -> int:
    """At-bats from an official or stats-normalized batting row.

    Official API: HitCnt is AB when HittingCnt is present; PlateAppearances is PA.
    stats.cpbl normalized rows put AB into PlateAppearances and omit HittingCnt.
    """
    if row.get("HittingCnt") is not None:
        try:
            return int(row.get("HitCnt") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(row.get("PlateAppearances") or 0)
    except (TypeError, ValueError):
        return 0


def _recent_batting_form(game_rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Skip 0-AB appearances (PR / defensive replacements).
    with_ab = [game for game in game_rows if int(game.get("atBats") or 0) > 0]
    recent3 = with_ab[:3]
    recent5 = with_ab[:5]

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
                    "hits": _box_row_hits(row),
                    "atBats": _box_row_at_bats(row),
                    "seasonAvg": row.get("SeasonAvg"),
                    "homeRuns": int(row.get("HomeRunCnt") or 0),
                    "rbi": int(row.get("RunBattedInCnt") or row.get("RunBattedINCnt") or 0),
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

        # Prefer season index (stable across matchups) over single-game SeasonAvg.
        if trustworthy:
            at_bats = season.get("atBats")
            hits = season.get("hits")
            if at_bats is not None and hits is not None:
                copy["atBats"] = int(at_bats)
                copy["hits"] = int(hits)
                copy["abHits"] = f"{int(at_bats)}-{int(hits)}"

            avg_text = format_avg(season.get("avg"))
            if avg_text:
                copy["avg"] = avg_text

            obp_text = format_avg(season.get("obp"))
            if obp_text:
                copy["obp"] = obp_text

            ops_text = format_ops(season.get("ops"))
            if ops_text:
                copy["ops"] = ops_text

            if season.get("homeRuns") is not None:
                copy["homeRuns"] = int(season["homeRuns"])
            if season.get("rbi") is not None:
                copy["rbi"] = int(season["rbi"])
        elif box_avg_by_acnt and acnt in box_avg_by_acnt:
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
    enriched: list[dict[str, Any]] = []
    for batter in batters:
        copy = dict(batter)
        hitter_acnt = str(copy.get("id") or "")
        if pitcher_acnt and hitter_acnt:
            copy.update(
                lookup_vs_pitcher_avgs(
                    pairs,
                    hitter_acnt=hitter_acnt,
                    pitcher_acnt=pitcher_acnt,
                )
            )
        else:
            copy.setdefault("vsPitcherSeasonAvg", None)
            copy.setdefault("vsPitcherCareerAvg", None)
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


def invalidate_shared_schedule_cache(*, wipe_disk: bool = False) -> None:
    global _shared_schedule, _shared_box_cache
    _shared_schedule = None
    _shared_box_cache.clear()
    if wipe_disk:
        try:
            SCHEDULE_CACHE_FILE.unlink(missing_ok=True)
        except OSError:
            pass


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
                # Always re-sync current (+ next) month so live vs Final stays accurate.
                try:
                    await self._sync_near_term_schedule(_shared_schedule)
                    _save_schedule_disk(_shared_schedule)
                except Exception:
                    logger.exception("CPBL near-term schedule sync failed")

        games = _shared_schedule
        if not isinstance(games, list):
            return []
        repaired = False
        for game in games:
            before = game.get("status")
            _repair_cached_game_status(game)
            if game.get("status") != before:
                repaired = True
        if repaired:
            _save_schedule_disk(games)
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

        try:
            from app.playsport_starters import fetch_playsport_starters
            from app.cpbl_teams import match_team

            playsport_games = await fetch_playsport_starters(self._http)
            for pg in playsport_games:
                if pg.get("league") != "cpbl":
                    continue
                away_t = match_team(pg.get("awayTeam", ""))
                home_t = match_team(pg.get("homeTeam", ""))
                if not away_t or not home_t:
                    continue
                pg_date = pg.get("gameDate")
                for g in games:
                    if g.get("awayTeamId") != away_t["id"] or g.get("homeTeamId") != home_t["id"]:
                        continue
                    if pg_date and g.get("date") and g.get("date") != pg_date:
                        continue
                    if not g.get("awayProbablePitcher") and pg.get("awayStarter"):
                        g["awayProbablePitcher"] = pg["awayStarter"]
                    if not g.get("homeProbablePitcher") and pg.get("homeStarter"):
                        g["homeProbablePitcher"] = pg["homeStarter"]
        except Exception:
            logger.exception("playsport CPBL enrich failed")

        return games

    def _merge_normalized_schedule_game(
        self, games: list[dict[str, Any]], by_sno: dict[Any, dict[str, Any]], normalized: dict[str, Any]
    ) -> None:
        game_sno = normalized.get("gameSno")
        if game_sno is None:
            return
        existing = by_sno.get(game_sno)
        if existing is None:
            games.append(normalized)
            by_sno[game_sno] = normalized
            return
        existing["status"] = normalized["status"]
        existing["awayScore"] = normalized.get("awayScore")
        existing["homeScore"] = normalized.get("homeScore")
        existing["hasDecision"] = normalized.get("hasDecision")
        existing["isPlayBall"] = normalized.get("isPlayBall")
        existing["stadium"] = normalized.get("stadium") or existing.get("stadium") or ""
        # Live schedule rows often blank pitcher names — keep known enrichment.
        if normalized.get("awayProbablePitcher"):
            existing["awayProbablePitcher"] = normalized["awayProbablePitcher"]
        if normalized.get("homeProbablePitcher"):
            existing["homeProbablePitcher"] = normalized["homeProbablePitcher"]

    async def _fetch_month_payload(
        self, year_value: int, month_value: int, *, schedule_page: httpx.Response | None = None
    ) -> tuple[dict[str, Any] | None, httpx.Response | None]:
        try:
            if schedule_page is None:
                schedule_page = await self._fetch_page(SCHEDULE_PAGE)
            payload = await self._post_with_csrf(
                SCHEDULE_PAGE,
                "/schedule/getgamedatas",
                {"year": year_value, "month": month_value, "kindCode": KIND_CODE},
                page=schedule_page,
            )
            return payload, schedule_page
        except (httpx.HTTPError, ValueError):
            try:
                schedule_page = await self._fetch_page(SCHEDULE_PAGE)
                payload = await self._post_with_csrf(
                    SCHEDULE_PAGE,
                    "/schedule/getgamedatas",
                    {"year": year_value, "month": month_value, "kindCode": KIND_CODE},
                    page=schedule_page,
                )
                return payload, schedule_page
            except (httpx.HTTPError, ValueError):
                return None, schedule_page

    async def _sync_near_term_schedule(self, games: list[dict[str, Any]]) -> None:
        """Refresh current and next month so In Progress vs Final stays correct."""
        today = date.today()
        months = [(today.year, today.month)]
        next_year, next_month = today.year, today.month + 1
        if next_month > 12:
            next_month = 1
            next_year += 1
        months.append((next_year, next_month))

        by_sno = {g.get("gameSno"): g for g in games if g.get("gameSno") is not None}
        schedule_page: httpx.Response | None = None
        for year_value, month_value in months:
            payload, schedule_page = await self._fetch_month_payload(
                year_value, month_value, schedule_page=schedule_page
            )
            if not payload or not payload.get("Success"):
                continue
            for raw in _parse_json_blob(payload.get("GameDatas")):
                normalized = _normalize_schedule_game(raw)
                if normalized:
                    self._merge_normalized_schedule_game(games, by_sno, normalized)
            await asyncio.sleep(0.05)
        games.sort(key=lambda game: (game.get("date", ""), game.get("gameSno") or 0))

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
        await _enrich_box_pitch_counts(self, parsed, game_sno, year)
        schedule = await self.fetch_schedule_pool()
        for game in schedule:
            if game.get("gameSno") == game_sno:
                parsed["awayTeamId"] = game["awayTeamId"]
                parsed["homeTeamId"] = game["homeTeamId"]
                parsed["date"] = game.get("date")
                break

        _shared_box_cache[cache_key] = parsed
        return parsed


def _merge_stats_pitch_counts(
    pitching: list[dict[str, Any]], stats_pitching: list[dict[str, Any]]
) -> None:
    if not pitching or not stats_pitching:
        return
    by_name: dict[str, dict[str, Any]] = {}
    for row in stats_pitching:
        name = (row.get("PitcherName") or "").strip()
        if name:
            by_name[name] = row
    for row in pitching:
        name = (row.get("PitcherName") or "").strip()
        if not name:
            continue
        stats_row = by_name.get(name)
        if not stats_row:
            for key, value in by_name.items():
                if _pitcher_name_matches(name, key):
                    stats_row = value
                    break
        if not stats_row:
            continue
        for key in ("PitchCnt", "pitchCnt", "BallCnt", "ballCnt"):
            if row.get(key) is None and stats_row.get(key) is not None:
                row[key] = stats_row.get(key)


async def _enrich_box_pitch_counts(
    client: CpblClient, parsed: dict[str, Any], game_sno: int | str, year: int
) -> None:
    from app.cpbl_stats import _fetch_stats_game_html, parse_stats_pitching

    try:
        html = await _fetch_stats_game_html(client._http, int(game_sno), int(year))
        stats_pitching = parse_stats_pitching(html, game_sno=int(game_sno), year=int(year))
        _merge_stats_pitch_counts(parsed.get("pitching") or [], stats_pitching)
    except Exception:
        logger.debug("CPBL pitch-count enrich failed for game %s", game_sno, exc_info=True)


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

    def sort_key(game: dict[str, Any]) -> tuple[int, str, int, Any]:
        game_date = game.get("date") or "9999-99-99"
        # Prefer live games over later scheduled shells on the same day.
        live_rank = 0 if game.get("status") in {"In Progress", "Live"} else 1
        has_pitchers = int(
            not (game.get("awayProbablePitcher") and game.get("homeProbablePitcher"))
        )
        return (live_rank, game_date, has_pitchers, game.get("gameSno") or 0)

    pool.sort(key=sort_key)
    game = pool[0]

    # Live schedule rows often blank Visiting/HomePitcherName — pull from box.
    if (
        game.get("status") in {"In Progress", "Live", "Scheduled"}
        and game.get("gameSno") is not None
        and (not game.get("awayProbablePitcher") or not game.get("homeProbablePitcher"))
    ):
        try:
            box = await client.fetch_box(
                int(game["gameSno"]), int(game.get("year") or date.today().year)
            )
        except (TypeError, ValueError):
            box = None
        if box:
            if not game.get("awayProbablePitcher") and box.get("awayStarter"):
                game["awayProbablePitcher"] = box["awayStarter"]
            if not game.get("homeProbablePitcher") and box.get("homeStarter"):
                game["homeProbablePitcher"] = box["homeStarter"]

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
        for index, runs in enumerate(inning_runs):
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
        row = _build_team_scoring_row(meta, box, team_id, include_scored=True)
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
        if _row_side_type(row) != side_type:
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
    line = _starter_pitching_line(box, team_id, pitcher_name)
    earned_runs = None
    run_cnt = None
    if line:
        earned_runs = line.get("EarnedRunCnt")
        if earned_runs is None:
            earned_runs = line.get("RunCnt")
        run_cnt = line.get("RunCnt")
    # Non-empty liveLog that never matches the starter yields all zeros while
    # the pitching line still has ER/R — fall back to innings-pitched heuristic.
    if used_live_log and sum(runs_by_inning) == 0:
        er_val = int(earned_runs or 0)
        r_val = int(run_cnt or 0)
        if er_val > 0 or r_val > 0:
            used_live_log = False
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

    innings_pitched = line.get("InningPitchedCnt") if line else None

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
        "pitchCount": pitch_count_from_cpbl_line(line),
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
    # Scan past game_count so skipped empty boxes still allow a full window.
    candidates = finished[: max(game_count * 3, game_count)]

    rows: list[dict[str, Any]] = []
    for meta in candidates:
        if len(rows) >= game_count:
            break
        box = await client.fetch_box(meta["gameSno"], int(meta.get("year") or date.today().year))
        if not box or not _box_has_inning_data(box):
            continue
        # Prefer schedule side; box team ids can be missing on some fallbacks.
        is_home = meta["homeTeamId"] == team_id
        side = "home" if is_home else "away"
        opp_side = "away" if is_home else "home"
        my_innings = box.get(f"{side}Innings") or []
        opp_innings = box.get(f"{opp_side}Innings") or []

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
                "date": meta.get("date"),
                "gameSno": meta.get("gameSno"),
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
        batters = _parse_lineup_from_box(
            preferred_box, visiting_home_type=visiting_home_type
        )
        # Today's box: full firstSno or live batting 先發 order both count as confirmed.
        if _lineup_is_complete(batters):
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
    # Official firstSno is often incomplete; batting 先發 order or older cards fill gaps.
    best_incomplete: tuple[list[dict[str, Any]], str | None] | None = None
    for meta in finished[:12]:
        box = await client.fetch_box(int(meta["gameSno"]), int(meta.get("year") or date.today().year))
        if not box:
            continue
        side_type = 2 if meta["homeTeamId"] == team_id else 1
        batters = _parse_lineup_from_box(box, visiting_home_type=side_type)
        if not batters:
            continue
        if _lineup_is_complete(batters):
            return batters, "previous", meta.get("date")
        if best_incomplete is None or len(batters) > len(best_incomplete[0]):
            best_incomplete = (batters, meta.get("date"))

    if best_incomplete:
        return best_incomplete[0], "previous", best_incomplete[1]
    return [], "previous", None


async def fetch_matchup_starting_lineups(
    client: CpblClient, matchup: dict[str, Any]
) -> dict[str, Any]:
    upcoming_box: dict[str, Any] | None = None
    game_sno = matchup.get("gameSno")
    year = int(matchup.get("year") or date.today().year)
    if game_sno is not None:
        upcoming_box = await client.fetch_box(game_sno, year)

    seed_map = await _pitcher_acnt_map_for_box(client, upcoming_box)
    needed_pitchers: list[str] = []
    for side_key in ("away", "home"):
        opposing = _resolve_opposing_pitcher(matchup, side_key=side_key)
        name = (opposing or {}).get("fullName") if opposing else None
        if name:
            needed_pitchers.append(str(name))
    pitcher_name_map = await _build_pitcher_name_acnt_map(
        client,
        year=year,
        seed_map=seed_map,
        needed_names=needed_pitchers,
    )
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
            confirmed = _parse_lineup_from_box(
                preferred_box, visiting_home_type=preferred_side_type
            )
            if _lineup_is_complete(confirmed):
                batters = confirmed

        # Drop corrupted cards (repeated batting orders from mid-game firstSno).
        if batters and not _lineup_orders_unique(batters):
            batters = []
        if batters and len(batters) >= 9 and not _lineup_is_complete(batters):
            batters = []

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

            # Pull enough team games so regulars still get 5 appearances with AB.
            recent_boxes = await _recent_team_boxes(client, team_id, limit=12)
            logs_by_acnt = _collect_batter_game_logs(recent_boxes, team_id)
            batters = _enrich_batters_with_recent_form(batters, logs_by_acnt)

            opposing = _resolve_opposing_pitcher(matchup, side_key=side_key)
            pitcher_name = (opposing or {}).get("fullName") if opposing else None
            pitcher_acnt = _lookup_pitcher_acnt(pitcher_name_map, pitcher_name)
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
    lineups["logicVersion"] = CPBL_LINEUP_LOGIC_VERSION
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
        away_panel, home_panel, away_table, home_table, starting_lineups = await asyncio.gather(
            _build_side_panel(client, matchup["away"], game_count),
            _build_side_panel(client, matchup["home"], game_count),
            fetch_inning_comparison(client, away_id),
            fetch_inning_comparison(client, home_id),
            fetch_matchup_starting_lineups(client, matchup),
        )
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


async def rebuild_pitcher_dependent_fields(
    data: dict[str, Any], *, game_count: int = 10
) -> dict[str, Any]:
    """Recompute lineups (incl. vs-pitcher) + situational after starters are known.

    Header-only updates used to set probablePitcher names without refreshing
    these derived blocks — leaving empty 對戰打擊率 and 客/主場先發情境.
    """
    matchup = data.get("matchup") or {}
    away = data.get("away") or {}
    home = data.get("home") or {}
    if not away.get("teamId") or not home.get("teamId"):
        return data

    old_lineups = data.get("startingLineups") or {}
    old_sit = data.get("situational") or {}

    lineup_matchup = {
        "date": matchup.get("date"),
        "gameDate": matchup.get("gameDate"),
        "gameSno": matchup.get("gameSno"),
        "year": int(str(matchup.get("date") or date.today().isoformat())[:4]),
        "status": matchup.get("status"),
        "stadium": matchup.get("stadium"),
        "away": {
            "teamId": away["teamId"],
            "teamName": away.get("teamName") or "",
            "probablePitcher": away.get("probablePitcher"),
        },
        "home": {
            "teamId": home["teamId"],
            "teamName": home.get("teamName") or "",
            "probablePitcher": home.get("probablePitcher"),
        },
    }

    client = CpblClient()
    try:
        lineups_task = asyncio.create_task(
            fetch_matchup_starting_lineups(client, lineup_matchup)
        )

        async def _pitcher_block(panel: dict[str, Any]) -> dict[str, Any] | None:
            probable = panel.get("probablePitcher") or {}
            name = (probable.get("fullName") or "").strip()
            if not name:
                return None
            return await analyze_pitcher_starts(
                client,
                name,
                int(panel["teamId"]),
                game_count,
                scan_limit=40,
            )

        away_pa, home_pa, starting_lineups = await asyncio.gather(
            _pitcher_block(away),
            _pitcher_block(home),
            lineups_task,
        )
        away = dict(away)
        home = dict(home)
        away["pitcherAnalysis"] = away_pa
        home["pitcherAnalysis"] = home_pa

        new_bad = cpbl_lineups_need_rebuild(starting_lineups)
        old_bad = cpbl_lineups_need_rebuild(old_lineups)
        if new_bad and not old_bad:
            starting_lineups = old_lineups
            vs_pairs = await get_vs_pitcher_lookup(lineup_matchup["year"])
            pitcher_map = _load_pitcher_acnt_disk()
            for side_key in ("away", "home"):
                side_lu = (starting_lineups or {}).get(side_key) or {}
                bats = side_lu.get("batters") or []
                if not bats:
                    continue
                opposing = _resolve_opposing_pitcher(lineup_matchup, side_key=side_key)
                pname = (opposing or {}).get("fullName") if opposing else None
                pacnt = _lookup_pitcher_acnt(pitcher_map, pname)
                side_lu = dict(side_lu)
                side_lu["batters"] = _enrich_batters_with_vs_pitcher(
                    bats, pairs=vs_pairs, pitcher_acnt=pacnt
                )
                side_lu["opposingPitcher"] = opposing
                starting_lineups = dict(starting_lineups or {})
                starting_lineups[side_key] = side_lu
            starting_lineups["logicVersion"] = CPBL_LINEUP_LOGIC_VERSION
        elif new_bad:
            # Drop duplicate-order garbage (1,1,1,2,2…) instead of showing it.
            starting_lineups = {
                "away": {"batters": []},
                "home": {"batters": []},
                "logicVersion": CPBL_LINEUP_LOGIC_VERSION,
            }
        elif starting_lineups is not None:
            starting_lineups = dict(starting_lineups)
            starting_lineups["logicVersion"] = CPBL_LINEUP_LOGIC_VERSION

        situational = build_matchup_situational(away, home)
        for key in ("awayTeamAwayGames", "homeTeamHomeGames"):
            new_block = situational.get(key) or {}
            old_block = old_sit.get(key) or {}
            if (new_block.get("gameCount") or 0) == 0 and (old_block.get("gameCount") or 0) > 0:
                situational[key] = old_block
        for key in ("awayPitcherAwayStarts", "homePitcherHomeStarts"):
            new_block = situational.get(key) or {}
            old_block = old_sit.get(key) or {}
            if (new_block.get("gameCount") or 0) == 0 and (old_block.get("gameCount") or 0) > 0:
                situational[key] = old_block

        away = strip_panel_internals(away)
        home = strip_panel_internals(home)
    finally:
        await client.close()

    data = dict(data)
    data["away"] = away
    data["home"] = home
    data["startingLineups"] = starting_lineups
    data["situational"] = situational
    return data
