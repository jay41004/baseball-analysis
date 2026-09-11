"""Cross-league data validation + CPBL auto-repair.

Catches logic bugs that look "complete" (wrong game, absurd avgs, bad lineups),
not only missing pitchers / thin panels.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_GAMES = 10

# Recent form this high over multiple games almost always means HitCnt/AB mix-up.
_ABSURD_RECENT_AVG = 0.800
_HIGH_RECENT_AVG = 0.650
_MIN_RECENT_GAMES_FOR_ABSURD = 2
_MIN_RECENT_GAMES_FOR_HIGH = 3

_TEAM_ID_RE = re.compile(r"^(?:mlb|npb|cpbl) team (\d+):", re.I)
_REPAIRABLE_MARKERS = (
    "missing _scoredPool",
    "situational gameCount",
    "source=confirmed but empty",
    "thin panels",
    "startingLineups are from another game",
    "wrong gamePk",
    "wrong matchup date",
)


def unwrap_matchup_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    inner = payload.get("data")
    if isinstance(inner, dict) and (
        "away" in inner or "home" in inner or "matchup" in inner
    ):
        return inner
    return payload


def pitcher_name(side: dict[str, Any] | None) -> str | None:
    if not side:
        return None
    p = side.get("probablePitcher")
    if isinstance(p, dict):
        name = (p.get("fullName") or "").strip()
        return name or None
    if isinstance(p, str) and p.strip():
        return p.strip()
    return None


def parse_avg(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"—", "-", "–", "N/A", "n/a", "."}:
        return None
    try:
        if text.startswith("."):
            return float("0" + text)
        return float(text)
    except (TypeError, ValueError):
        return None


def _lineup_orders(batters: list[dict[str, Any]]) -> list[int]:
    orders: list[int] = []
    for batter in batters:
        raw = batter.get("order") or batter.get("battingOrder") or batter.get("lineup")
        try:
            orders.append(int(raw))
        except (TypeError, ValueError):
            continue
    return orders


def audit_matchup_data(
    league: str,
    team_id: int,
    data: dict[str, Any],
    *,
    min_games: int = 5,
    today: str | None = None,
) -> dict[str, list[str]]:
    """Validate one matchup payload. Returns critical issues + warnings."""
    issues: list[str] = []
    warnings: list[str] = []
    today = today or date.today().isoformat()

    away = data.get("away") or {}
    home = data.get("home") or {}
    matchup = data.get("matchup") or {}
    mdate = (matchup.get("date") or "")[:10]
    status = (matchup.get("status") or "").strip()
    status_l = status.lower()

    ag = len(away.get("games") or [])
    hg = len(home.get("games") or [])
    if ag < min_games or hg < min_games:
        issues.append(f"{league} team {team_id}: thin panels away={ag} home={hg}")

    live_like = status_l in {"in progress", "live", "inprogress"}
    upcoming = mdate >= today and status_l in {
        "",
        "scheduled",
        "preview",
        "warmup",
        "pre-game",
        "pregame",
    }
    active = live_like or upcoming

    if active:
        if not pitcher_name(away) or not pitcher_name(home):
            # Live/scheduled without both names — warning for early announce, issue if live.
            msg = (
                f"{league} team {team_id}: {status or 'upcoming'} {mdate} missing starter(s) "
                f"away={pitcher_name(away)!r} home={pitcher_name(home)!r}"
            )
            if live_like:
                issues.append(msg)
            else:
                warnings.append(msg)

        for side_key, panel in (("away", away), ("home", home)):
            starter = pitcher_name(panel)
            if not starter:
                continue
            pa_games = ((panel.get("pitcherAnalysis") or {}).get("games") or [])
            if not pa_games:
                issues.append(
                    f"{league} team {team_id}: {side_key} starter {starter!r} "
                    f"missing pitcherAnalysis games"
                )
            elif len(pa_games) < 3:
                warnings.append(
                    f"{league} team {team_id}: {side_key} starter {starter!r} "
                    f"thin pitcherAnalysis (n={len(pa_games)})"
                )

    if mdate and mdate < today and status_l in {"scheduled", "preview", ""}:
        warnings.append(
            f"{league} team {team_id}: matchup date {mdate} is past but status={status!r}"
        )

    lineups = data.get("startingLineups") or {}
    from app.matchup_integrity import lineups_trusted_for_league

    if lineups and not lineups_trusted_for_league(
        lineups,
        league,
        matchup_date=mdate,
        matchup_status=status,
    ):
        has_cross_game_card = any(
            len((lineups.get(side) or {}).get("batters") or []) >= 7
            for side in ("away", "home")
        )
        if has_cross_game_card:
            issues.append(
                f"{league} team {team_id}: startingLineups are from another game or stale source"
            )

    for side_key in ("away", "home"):
        side_lineup = lineups.get(side_key) or {}
        batters = list(side_lineup.get("batters") or [])
        src = str(side_lineup.get("source") or "").lower()
        if not batters:
            if src == "confirmed":
                issues.append(
                    f"{league} team {team_id}: {side_key} lineup source=confirmed but empty"
                )
            elif active:
                warnings.append(
                    f"{league} team {team_id}: {side_key} lineup empty on active matchup"
                )
            continue

        orders = _lineup_orders(batters)
        if len(batters) >= 9:
            if len(orders) >= 9 and len(set(orders)) < len(orders):
                issues.append(
                    f"{league} team {team_id}: {side_key} lineup has duplicate orders {orders}"
                )
            if orders and sorted(orders)[:9] != list(range(1, 10)) and len(set(orders)) == 9:
                # Not exactly 1–9 but unique — soft.
                warnings.append(
                    f"{league} team {team_id}: {side_key} lineup orders {orders} (expect 1–9)"
                )
        elif active and len(batters) < 9:
            warnings.append(
                f"{league} team {team_id}: {side_key} lineup incomplete ({len(batters)} batters)"
            )

        for batter in batters:
            name = (
                batter.get("name")
                or batter.get("fullName")
                or batter.get("CHName")
                or "?"
            )
            for field in (
                "avg",
                "recent3Avg",
                "recent5Avg",
                "rispAvg",
                "vsPitcherSeasonAvg",
                "vsPitcherCareerAvg",
            ):
                raw = batter.get(field)
                avg = parse_avg(raw)
                if avg is None:
                    continue
                if avg < 0 or avg > 1.0:
                    issues.append(
                        f"{league} team {team_id}: {side_key} {name} {field}={raw!r} out of range"
                    )
                    continue
                if field not in {"recent3Avg", "recent5Avg"}:
                    continue
                games_key = "recent3Games" if field == "recent3Avg" else "recent5Games"
                sample = int(batter.get(games_key) or 0)
                # recent5 may omit games count — fall back to recent3Games.
                if field == "recent5Avg" and sample <= 0:
                    sample = int(batter.get("recent3Games") or 0)
                if avg >= _ABSURD_RECENT_AVG and sample >= _MIN_RECENT_GAMES_FOR_ABSURD:
                    issues.append(
                        f"{league} team {team_id}: {side_key} {name} {field}={raw!r} "
                        f"absurd for {sample} games (likely AB/H field bug)"
                    )
                elif avg >= _HIGH_RECENT_AVG and sample >= _MIN_RECENT_GAMES_FOR_HIGH:
                    warnings.append(
                        f"{league} team {team_id}: {side_key} {name} {field}={raw!r} "
                        f"very high for {sample} games"
                    )

            ab_hits = str(batter.get("abHits") or "")
            if "-" in ab_hits:
                try:
                    ab_s, h_s = ab_hits.split("-", 1)
                    ab_i, h_i = int(ab_s), int(h_s)
                    if h_i > ab_i and ab_i > 0:
                        issues.append(
                            f"{league} team {team_id}: {side_key} {name} abHits={ab_hits} (H>AB)"
                        )
                except (TypeError, ValueError):
                    pass

    if league in {"mlb", "npb"}:
        from app.inning_comparison import build_matchup_situational

        for side_key, panel in (("away", away), ("home", home)):
            game_n = len(panel.get("games") or [])
            pool_n = len(panel.get("_scoredPool") or [])
            if game_n >= min_games and pool_n < min_games:
                issues.append(
                    f"{league} team {team_id}: {side_key} missing _scoredPool "
                    f"(games={game_n} pool={pool_n})"
                )

        sit = data.get("situational") or {}
        if away.get("_scoredPool") or home.get("_scoredPool"):
            expected_sit = build_matchup_situational(away, home)
            for key in ("awayTeamAwayGames", "homeTeamHomeGames"):
                got = int((sit.get(key) or {}).get("gameCount") or 0)
                exp = int((expected_sit.get(key) or {}).get("gameCount") or 0)
                if exp >= min_games and got < exp:
                    issues.append(
                        f"{league} team {team_id}: situational gameCount {key} "
                        f"cached={got} expected={exp} (location pool mismatch)"
                    )

    if league == "cpbl" and active:
        sit = data.get("situational") or {}
        for key, label in (
            ("awayPitcherAwayStarts", "客場先發情境"),
            ("homePitcherHomeStarts", "主場先發情境"),
        ):
            side = away if key.startswith("away") else home
            if pitcher_name(side) and not ((sit.get(key) or {}).get("gameCount") or 0):
                warnings.append(
                    f"{league} team {team_id}: {label} empty despite starter "
                    f"{pitcher_name(side)!r}"
                )

    return {"issues": issues, "warnings": warnings}


def team_ids_from_issues(issues: list[str], league: str) -> list[int]:
    prefix = f"{league} team "
    out: list[int] = []
    seen: set[int] = set()
    for msg in issues:
        if not msg.lower().startswith(prefix):
            continue
        match = _TEAM_ID_RE.match(msg)
        if not match:
            continue
        tid = int(match.group(1))
        if tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def _is_repairable_issue(msg: str) -> bool:
    return any(marker in msg for marker in _REPAIRABLE_MARKERS)


def _repairable_team_ids(issues: list[str], league: str) -> list[int]:
    prefix = f"{league} team "
    out: list[int] = []
    for tid in team_ids_from_issues(issues, league):
        team_prefix = f"{prefix}{tid}:"
        if any(_is_repairable_issue(msg) and msg.startswith(team_prefix) for msg in issues):
            out.append(tid)
    return out


async def expected_mlb_matchups() -> dict[int, dict[str, Any]]:
    """Live schedule truth: team_id → fetch_next_matchup result."""
    import httpx

    from app.mlb_service import fetch_next_matchup, fetch_teams

    async with httpx.AsyncClient(timeout=30.0) as client:
        teams = await fetch_teams()
        out: dict[int, dict[str, Any]] = {}
        for team in teams:
            tid = int(team["id"])
            try:
                matchup = await fetch_next_matchup(client, tid)
            except Exception:
                logger.exception("expected_mlb_matchups failed for team %s", tid)
                continue
            if matchup:
                out[tid] = matchup
        return out


def audit_mlb_against_expected(
    team_id: int,
    data: dict[str, Any],
    expected: dict[str, Any] | None,
) -> dict[str, list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    if not expected:
        warnings.append(f"mlb team {team_id}: no live schedule matchup to compare")
        return {"issues": issues, "warnings": warnings}

    matchup = data.get("matchup") or {}
    exp_pk = expected.get("gamePk")
    got_pk = matchup.get("gamePk")
    exp_date = (expected.get("date") or "")[:10]
    got_date = (matchup.get("date") or "")[:10]
    exp_status = (expected.get("status") or "").strip()

    if exp_pk is not None and got_pk is not None and exp_pk != got_pk:
        issues.append(
            f"mlb team {team_id}: wrong gamePk cached={got_pk} expected={exp_pk} "
            f"(cached {got_date} vs live {exp_date} {exp_status})"
        )
    elif exp_date and got_date and exp_date != got_date:
        issues.append(
            f"mlb team {team_id}: wrong matchup date cached={got_date} expected={exp_date}"
        )

    for side in ("away", "home"):
        exp_name = pitcher_name(expected.get(side) or {})
        got_name = pitcher_name(data.get(side) or {})
        exp_team = (expected.get(side) or {}).get("teamId")
        got_team = (data.get(side) or {}).get("teamId")
        if exp_team and got_team and int(exp_team) != int(got_team):
            issues.append(
                f"mlb team {team_id}: {side} teamId cached={got_team} expected={exp_team}"
            )
        if exp_name and got_name and exp_name != got_name:
            issues.append(
                f"mlb team {team_id}: {side} pitcher cached={got_name!r} expected={exp_name!r}"
            )
        elif exp_name and not got_name:
            status_l = exp_status.lower()
            if status_l in {"in progress", "live"}:
                issues.append(
                    f"mlb team {team_id}: {side} pitcher missing (live expects {exp_name!r})"
                )
            else:
                warnings.append(
                    f"mlb team {team_id}: {side} pitcher missing (expects {exp_name!r})"
                )

    return {"issues": issues, "warnings": warnings}


async def repair_mlb_cache_from_live(*, games: int = DEFAULT_GAMES) -> dict[str, Any]:
    """Force header+panel refresh for all MLB teams against live schedule."""
    from app.cache import get_matchup, load_from_disk
    from app.scheduler import refresh_matchup, refresh_matchup_header

    load_from_disk()
    from app.mlb_service import fetch_teams

    teams = await fetch_teams()
    repaired: list[int] = []
    full_refreshed: list[int] = []
    failed: list[int] = []
    for team in teams:
        tid = int(team["id"])
        try:
            await refresh_matchup_header(tid, games)
            entry = get_matchup(tid, games)
            data = unwrap_matchup_payload(entry)
            away_n = len((data.get("away") or {}).get("games") or [])
            home_n = len((data.get("home") or {}).get("games") or [])
            if away_n < 5 or home_n < 5:
                await refresh_matchup(tid, games)
                full_refreshed.append(tid)
            repaired.append(tid)
        except Exception:
            logger.exception("MLB repair refresh failed for team %s", tid)
            failed.append(tid)

    still_wrong = 0
    expected = await expected_mlb_matchups()
    for team in teams:
        tid = int(team["id"])
        entry = get_matchup(tid, games)
        data = unwrap_matchup_payload(entry)
        cross = audit_mlb_against_expected(tid, data, expected.get(tid))
        base = audit_matchup_data("mlb", tid, data, min_games=5)
        still_wrong += len(cross["issues"]) + len(base["issues"])

    return {
        "repairedTeams": repaired,
        "fullRefreshedTeams": full_refreshed,
        "failedTeams": failed,
        "issuesRemaining": still_wrong,
    }


async def repair_mlb_cache_from_issues(
    issues: list[str], *, games: int = DEFAULT_GAMES
) -> dict[str, Any]:
    """Full refresh for MLB teams flagged by validation."""
    from app.cache import get_matchup, load_from_disk
    from app.scheduler import refresh_matchup, refresh_matchup_header

    load_from_disk()
    targets = _repairable_team_ids(issues, "mlb")
    refreshed: list[int] = []
    failed: list[int] = []
    for tid in targets:
        try:
            team_issues = [m for m in issues if m.startswith(f"mlb team {tid}:")]
            cross_issue = any(
                "wrong gamePk" in msg or "wrong matchup date" in msg
                for msg in team_issues
            )
            if cross_issue:
                await refresh_matchup_header(tid, games)
                entry = get_matchup(tid, games)
                data = unwrap_matchup_payload(entry)
                away_n = len((data.get("away") or {}).get("games") or [])
                home_n = len((data.get("home") or {}).get("games") or [])
                if away_n < 5 or home_n < 5:
                    await refresh_matchup(tid, games)
            else:
                await refresh_matchup(tid, games)
            refreshed.append(tid)
        except Exception:
            logger.exception("MLB repair refresh failed for team %s", tid)
            failed.append(tid)
    return {"refreshedTeams": refreshed, "failedTeams": failed}


async def expected_npb_matchups() -> dict[int, dict[str, Any]]:
    """Live NPB schedule truth: team_id → next matchup with probable pitchers."""
    from app.npb_service import NpbClient, fetch_next_matchup

    client = NpbClient()
    out: dict[int, dict[str, Any]] = {}
    try:
        for tid in range(1, 13):
            try:
                matchup = await fetch_next_matchup(client, tid)
            except Exception:
                logger.exception("expected_npb_matchups failed for team %s", tid)
                continue
            if not matchup:
                continue
            out[tid] = {
                "date": matchup.get("date"),
                "awayTeamId": int(matchup["away"]["teamId"]),
                "homeTeamId": int(matchup["home"]["teamId"]),
                "away": matchup.get("away") or {},
                "home": matchup.get("home") or {},
            }
    finally:
        await client.close()
    return out


def audit_npb_against_expected(
    team_id: int,
    data: dict[str, Any],
    expected: dict[str, Any] | None,
) -> dict[str, list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    if not expected:
        warnings.append(f"npb team {team_id}: no live schedule matchup to compare")
        return {"issues": issues, "warnings": warnings}

    matchup = data.get("matchup") or {}
    exp_date = str(expected.get("date") or "")[:10]
    got_date = str(matchup.get("date") or "")[:10]
    if exp_date and got_date and exp_date != got_date:
        issues.append(
            f"npb team {team_id}: wrong matchup date cached={got_date} expected={exp_date}"
        )

    for side in ("away", "home"):
        exp_name = pitcher_name(expected.get(side) or {})
        got_name = pitcher_name(data.get(side) or {})
        exp_team = int((expected.get(side) or {}).get("teamId") or 0)
        got_team = int((data.get(side) or {}).get("teamId") or 0)
        if exp_team and got_team and exp_team != got_team:
            issues.append(
                f"npb team {team_id}: {side} teamId cached={got_team} expected={exp_team}"
            )
        if exp_name and got_name and exp_name != got_name:
            issues.append(
                f"npb team {team_id}: {side} pitcher cached={got_name!r} expected={exp_name!r}"
            )
        elif exp_name and not got_name:
            issues.append(
                f"npb team {team_id}: {side} pitcher missing (expects {exp_name!r})"
            )
    return {"issues": issues, "warnings": warnings}


async def repair_npb_cache_from_issues(
    issues: list[str], *, games: int = DEFAULT_GAMES
) -> dict[str, Any]:
    """Full refresh for NPB teams flagged by validation."""
    from app.matchup_pick import ExpectedMatchup
    from app.npb_cache import get_matchup, load_from_disk
    from app.npb_scheduler import refresh_matchup

    load_from_disk()
    targets = _repairable_team_ids(issues, "npb")
    refreshed: list[int] = []
    failed: list[int] = []
    for tid in targets:
        try:
            entry = get_matchup(tid, games)
            expected = ExpectedMatchup.from_cache_entry(entry)
            await refresh_matchup(tid, games, expected=expected)
            refreshed.append(tid)
        except Exception:
            logger.exception("NPB repair refresh failed for team %s", tid)
            failed.append(tid)
    return {"refreshedTeams": refreshed, "failedTeams": failed}


async def expected_cpbl_matchups() -> dict[int, dict[str, Any]]:
    """Live schedule truth: team_id → fetch_next_matchup result."""
    from app.cpbl_service import CpblClient, fetch_next_matchup, invalidate_shared_schedule_cache

    invalidate_shared_schedule_cache(wipe_disk=False)
    client = CpblClient()
    out: dict[int, dict[str, Any]] = {}
    try:
        for tid in range(1, 7):
            try:
                matchup = await fetch_next_matchup(client, tid)
            except Exception:
                logger.exception("expected_cpbl_matchups failed for team %s", tid)
                continue
            if matchup:
                out[tid] = matchup
    finally:
        await client.close()
    return out


def audit_cpbl_against_expected(
    team_id: int,
    data: dict[str, Any],
    expected: dict[str, Any] | None,
) -> dict[str, list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    if not expected:
        warnings.append(f"cpbl team {team_id}: no live schedule matchup to compare")
        return {"issues": issues, "warnings": warnings}

    matchup = data.get("matchup") or {}
    exp_sno = expected.get("gameSno")
    got_sno = matchup.get("gameSno")
    exp_date = (expected.get("date") or "")[:10]
    got_date = (matchup.get("date") or "")[:10]
    exp_status = (expected.get("status") or "").strip()

    if exp_sno is not None and got_sno is not None and exp_sno != got_sno:
        issues.append(
            f"cpbl team {team_id}: wrong gameSno cached={got_sno} expected={exp_sno} "
            f"(cached {got_date} vs live {exp_date} {exp_status})"
        )
    elif exp_date and got_date and exp_date != got_date:
        issues.append(
            f"cpbl team {team_id}: wrong matchup date cached={got_date} expected={exp_date}"
        )

    for side in ("away", "home"):
        exp_name = pitcher_name(expected.get(side) or {})
        got_name = pitcher_name(data.get(side) or {})
        exp_team = (expected.get(side) or {}).get("teamId")
        got_team = (data.get(side) or {}).get("teamId")
        if exp_team and got_team and int(exp_team) != int(got_team):
            issues.append(
                f"cpbl team {team_id}: {side} teamId cached={got_team} expected={exp_team}"
            )
        if exp_name and got_name and exp_name != got_name:
            issues.append(
                f"cpbl team {team_id}: {side} pitcher cached={got_name!r} expected={exp_name!r}"
            )
        elif exp_name and not got_name:
            status_l = exp_status.lower()
            if status_l in {"in progress", "live"}:
                issues.append(
                    f"cpbl team {team_id}: {side} pitcher missing (live expects {exp_name!r})"
                )
            else:
                warnings.append(
                    f"cpbl team {team_id}: {side} pitcher missing (expects {exp_name!r})"
                )

    return {"issues": issues, "warnings": warnings}


async def repair_cpbl_cache_from_live(*, games: int = DEFAULT_GAMES) -> dict[str, Any]:
    """Force header+lineup refresh for all CPBL teams against live schedule."""
    from app.cpbl_cache import get_matchup, load_from_disk
    from app.cpbl_scheduler import refresh_matchup, refresh_matchup_header

    load_from_disk()
    repaired: list[int] = []
    full_refreshed: list[int] = []
    failed: list[int] = []
    for tid in range(1, 7):
        try:
            await refresh_matchup_header(tid, games)
            entry = get_matchup(tid, games)
            data = unwrap_matchup_payload(entry)
            away_n = len((data.get("away") or {}).get("games") or [])
            home_n = len((data.get("home") or {}).get("games") or [])
            # Header-only leaves empty scoring panels when the matchup club pair changed.
            if away_n < 5 or home_n < 5:
                await refresh_matchup(tid, games)
                full_refreshed.append(tid)
            repaired.append(tid)
        except Exception:
            logger.exception("CPBL repair refresh failed for team %s", tid)
            failed.append(tid)

    still_wrong = 0
    expected = await expected_cpbl_matchups()
    for tid in range(1, 7):
        entry = get_matchup(tid, games)
        data = unwrap_matchup_payload(entry)
        cross = audit_cpbl_against_expected(tid, data, expected.get(tid))
        base = audit_matchup_data("cpbl", tid, data, min_games=5)
        still_wrong += len(cross["issues"]) + len(base["issues"])

    return {
        "repairedTeams": repaired,
        "fullRefreshedTeams": full_refreshed,
        "failedTeams": failed,
        "issuesRemaining": still_wrong,
    }


async def validate_cpbl_cache(
    *,
    games: int = DEFAULT_GAMES,
    repair: bool = True,
    offline: bool = False,
) -> dict[str, Any]:
    """Validate in-memory/disk CPBL cache; optionally auto-repair wrong games."""
    from app.cpbl_cache import get_matchup, load_from_disk

    load_from_disk()
    expected = {} if offline else await expected_cpbl_matchups()
    issues: list[str] = []
    warnings: list[str] = []

    for tid in range(1, 7):
        entry = get_matchup(tid, games)
        data = unwrap_matchup_payload(entry)
        if not data:
            issues.append(f"cpbl team {tid}: missing matchup cache")
            continue
        base = audit_matchup_data("cpbl", tid, data, min_games=5)
        cross = audit_cpbl_against_expected(tid, data, expected.get(tid))
        issues.extend(base["issues"])
        issues.extend(cross["issues"])
        warnings.extend(base["warnings"])
        warnings.extend(cross["warnings"])

    repair_result = None
    if repair and issues:
        logger.warning(
            "CPBL validation found %s issue(s); auto-repairing", len(issues)
        )
        for msg in issues[:12]:
            logger.warning("  before-repair: %s", msg)
        repair_result = await repair_cpbl_cache_from_live(games=games)
        # Re-validate
        expected = await expected_cpbl_matchups()
        issues = []
        warnings = []
        for tid in range(1, 7):
            entry = get_matchup(tid, games)
            data = unwrap_matchup_payload(entry)
            if not data:
                issues.append(f"cpbl team {tid}: missing matchup cache")
                continue
            base = audit_matchup_data("cpbl", tid, data, min_games=5)
            cross = audit_cpbl_against_expected(tid, data, expected.get(tid))
            issues.extend(base["issues"])
            issues.extend(cross["issues"])
            warnings.extend(base["warnings"])
            warnings.extend(cross["warnings"])

    ok = len(issues) == 0
    report = {
        "ok": ok,
        "issues": issues,
        "warnings": warnings,
        "repair": repair_result,
        "expectedGames": {
            str(tid): {
                "date": (m.get("date") or "")[:10],
                "gameSno": m.get("gameSno"),
                "status": m.get("status"),
            }
            for tid, m in expected.items()
        },
    }
    if issues:
        for msg in issues:
            logger.error("CPBL validate: %s", msg)
    for msg in warnings[:20]:
        logger.warning("CPBL validate: %s", msg)
    return report


def _audit_cached_league(
    league: str,
    *,
    team_ids: list[int],
    get_entry,
    min_games: int = 5,
) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    missing = 0
    for tid in team_ids:
        entry = get_entry(tid)
        data = unwrap_matchup_payload(entry)
        if not data:
            missing += 1
            issues.append(f"{league} team {tid}: missing matchup cache")
            continue
        result = audit_matchup_data(league, tid, data, min_games=min_games)
        issues.extend(result["issues"])
        warnings.extend(result["warnings"])
    return {
        "ok": len(issues) == 0,
        "league": league,
        "teamsChecked": len(team_ids),
        "missing": missing,
        "issues": issues,
        "warnings": warnings,
    }


def _cached_team_ids_from_disk(league: str, games: int) -> list[int]:
    """Team IDs present in on-disk cache (no network)."""
    if league == "cpbl":
        return list(range(1, 7))
    if league == "mlb":
        from app.cache import CACHE_VERSION, load_from_disk
        import app.cache as cache_mod

        load_from_disk()
        prefix = f"matchup:v{CACHE_VERSION}:"
        suffix = f":{games}"
        store = cache_mod._store
    elif league == "npb":
        from app.npb_cache import load_from_disk
        import app.npb_cache as cache_mod

        load_from_disk()
        prefix = cache_mod._key_prefix()
        suffix = f":{games}"
        store = cache_mod._store
    else:
        return []

    ids: list[int] = []
    for key in store:
        if not (key.startswith(prefix) and key.endswith(suffix)):
            continue
        middle = key[len(prefix) : -len(suffix)]
        try:
            ids.append(int(middle))
        except ValueError:
            continue
    return sorted(set(ids))


async def validate_npb_cache(
    *, games: int = DEFAULT_GAMES, repair: bool = False, offline: bool = False
) -> dict[str, Any]:
    from app.npb_cache import get_matchup, load_from_disk

    load_from_disk()
    if offline:
        ids = _cached_team_ids_from_disk("npb", games)
        expected: dict[int, dict[str, Any]] = {}
    else:
        from app.npb_service import fetch_npb_teams

        teams = await fetch_npb_teams()
        ids = [int(t["id"]) for t in teams]
        expected = await expected_npb_matchups()

    issues: list[str] = []
    warnings: list[str] = []
    for tid in ids:
        entry = get_matchup(tid, games)
        data = unwrap_matchup_payload(entry)
        if not data:
            issues.append(f"npb team {tid}: missing matchup cache")
            continue
        base = audit_matchup_data("npb", tid, data, min_games=5)
        cross = audit_npb_against_expected(tid, data, expected.get(tid))
        issues.extend(base["issues"])
        issues.extend(cross["issues"])
        warnings.extend(base["warnings"])
        warnings.extend(cross["warnings"])

    report = {
        "ok": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "teamCount": len(ids),
    }
    repair_result = None
    if repair and report.get("issues"):
        logger.warning(
            "NPB validation found %s issue(s); auto-repairing",
            len(report["issues"]),
        )
        repair_result = await repair_npb_cache_from_issues(report["issues"], games=games)
        report = _audit_cached_league(
            "npb",
            team_ids=ids,
            get_entry=lambda tid: get_matchup(tid, games),
            min_games=5,
        )
    if repair_result is not None:
        report["repair"] = repair_result
    return report


async def validate_mlb_cache(
    *, games: int = DEFAULT_GAMES, repair: bool = False, offline: bool = False
) -> dict[str, Any]:
    from app.cache import get_matchup, load_from_disk

    load_from_disk()
    if offline:
        ids = _cached_team_ids_from_disk("mlb", games)
        expected: dict[int, dict[str, Any]] = {}
    else:
        from app.mlb_service import fetch_teams

        teams = await fetch_teams()
        ids = [int(t["id"]) for t in teams]
        expected = await expected_mlb_matchups()

    issues: list[str] = []
    warnings: list[str] = []
    for tid in ids:
        entry = get_matchup(tid, games)
        data = unwrap_matchup_payload(entry)
        if not data:
            issues.append(f"mlb team {tid}: missing matchup cache")
            continue
        base = audit_matchup_data("mlb", tid, data, min_games=5)
        cross = audit_mlb_against_expected(tid, data, expected.get(tid))
        issues.extend(base["issues"])
        issues.extend(cross["issues"])
        warnings.extend(base["warnings"])
        warnings.extend(cross["warnings"])

    report = {
        "ok": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "teamCount": len(ids),
    }
    repair_result = None
    if repair and issues:
        cross_issues = [
            m
            for m in issues
            if "wrong gamePk" in m or "wrong matchup date" in m
        ]
        logger.warning(
            "MLB validation found %s issue(s); auto-repairing (%s cross-game)",
            len(issues),
            len(cross_issues),
        )
        if cross_issues:
            repair_result = await repair_mlb_cache_from_live(games=games)
        else:
            repair_result = await repair_mlb_cache_from_issues(issues, games=games)
        issues = []
        warnings = []
        for tid in ids:
            entry = get_matchup(tid, games)
            data = unwrap_matchup_payload(entry)
            if not data:
                issues.append(f"mlb team {tid}: missing matchup cache")
                continue
            base = audit_matchup_data("mlb", tid, data, min_games=5)
            cross = audit_mlb_against_expected(tid, data, expected.get(tid))
            issues.extend(base["issues"])
            issues.extend(cross["issues"])
            warnings.extend(base["warnings"])
            warnings.extend(cross["warnings"])
        report = {
            "ok": len(issues) == 0,
            "issues": issues,
            "warnings": warnings,
            "teamCount": len(ids),
        }
    if repair_result is not None:
        report["repair"] = repair_result
    return report


async def validate_all_caches(
    *,
    repair_cpbl: bool = True,
    repair_mlb: bool = False,
    repair_npb: bool = False,
    games: int = DEFAULT_GAMES,
    offline: bool = False,
) -> dict[str, Any]:
    cpbl = await validate_cpbl_cache(repair=repair_cpbl, games=games, offline=offline)
    npb = await validate_npb_cache(repair=repair_npb, games=games, offline=offline)
    mlb = await validate_mlb_cache(repair=repair_mlb, games=games, offline=offline)
    critical = list(cpbl.get("issues") or []) + list(npb.get("issues") or []) + list(
        mlb.get("issues") or []
    )
    return {
        "ok": len(critical) == 0,
        "critical": critical,
        "cpbl": cpbl,
        "npb": npb,
        "mlb": mlb,
    }