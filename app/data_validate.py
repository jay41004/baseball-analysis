"""Cross-league data validation + CPBL auto-repair.

Catches logic bugs that look "complete" (wrong game, absurd avgs, bad lineups),
not only missing pitchers / thin panels.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_GAMES = 10

# Recent form this high over multiple games almost always means HitCnt/AB mix-up.
_ABSURD_RECENT_AVG = 0.800
_HIGH_RECENT_AVG = 0.650
_MIN_RECENT_GAMES_FOR_ABSURD = 2
_MIN_RECENT_GAMES_FOR_HIGH = 3


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

    if mdate and mdate < today and status_l in {"scheduled", "preview", ""}:
        warnings.append(
            f"{league} team {team_id}: matchup date {mdate} is past but status={status!r}"
        )

    lineups = data.get("startingLineups") or {}
    for side_key in ("away", "home"):
        batters = list((lineups.get(side_key) or {}).get("batters") or [])
        if not batters:
            if active:
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
) -> dict[str, Any]:
    """Validate in-memory/disk CPBL cache; optionally auto-repair wrong games."""
    from app.cpbl_cache import get_matchup, load_from_disk

    load_from_disk()
    expected = await expected_cpbl_matchups()
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


async def validate_npb_cache(*, games: int = DEFAULT_GAMES) -> dict[str, Any]:
    from app.npb_cache import get_matchup, load_from_disk
    from app.npb_service import fetch_npb_teams

    load_from_disk()
    teams = await fetch_npb_teams()
    ids = [int(t["id"]) for t in teams]
    return _audit_cached_league(
        "npb",
        team_ids=ids,
        get_entry=lambda tid: get_matchup(tid, games),
        min_games=5,
    )


async def validate_mlb_cache(*, games: int = DEFAULT_GAMES) -> dict[str, Any]:
    from app.cache import get_matchup, load_from_disk
    from app.mlb_service import fetch_teams

    load_from_disk()
    teams = await fetch_teams()
    ids = [int(t["id"]) for t in teams]
    return _audit_cached_league(
        "mlb",
        team_ids=ids,
        get_entry=lambda tid: get_matchup(tid, games),
        min_games=5,
    )


async def validate_all_caches(*, repair_cpbl: bool = True) -> dict[str, Any]:
    cpbl = await validate_cpbl_cache(repair=repair_cpbl)
    npb = await validate_npb_cache()
    mlb = await validate_mlb_cache()
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