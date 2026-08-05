"""Audit static/cache data after refresh; auto-repair CPBL when wrong/missing.

Runs in GitHub Actions and local refresh. Validates panels, lineups, batting
avgs, and (for CPBL) live schedule gameSno/pitchers — not only missing names.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("audit_repair")

DEFAULT_GAMES = 10


def _load_matchups(data_dir: Path, league: str) -> list[tuple[int, dict[str, Any]]]:
    from app.data_validate import unwrap_matchup_payload

    out: list[tuple[int, dict[str, Any]]] = []
    league_dir = data_dir / league
    if not league_dir.is_dir():
        return out
    for path in sorted(league_dir.glob(f"matchup_*_{DEFAULT_GAMES}.json")):
        try:
            tid = int(path.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append((tid, unwrap_matchup_payload(payload)))
    return out


def audit_league_files(
    data_dir: Path, league: str, *, min_teams: int, min_games: int
) -> dict[str, Any]:
    from app.data_validate import audit_matchup_data, pitcher_name

    issues: list[str] = []
    warnings: list[str] = []
    rows = _load_matchups(data_dir, league)
    if len(rows) < min_teams:
        issues.append(f"{league}: only {len(rows)} matchup files (need >={min_teams})")

    missing_pitchers = 0
    thin_panels = 0
    today = date.today().isoformat()

    for tid, data in rows:
        result = audit_matchup_data(
            league, tid, data, min_games=min_games, today=today
        )
        for msg in result["issues"]:
            if "thin panels" in msg:
                thin_panels += 1
            if "missing starter" in msg:
                missing_pitchers += 1
            issues.append(msg)
        warnings.extend(result["warnings"])
        for msg in result["warnings"]:
            if "missing starter" in msg:
                missing_pitchers += 1

    return {
        "league": league,
        "teams": len(rows),
        "thinPanels": thin_panels,
        "missingPitchers": missing_pitchers,
        "issues": issues,
        "warnings": warnings,
    }


async def _probe_cpbl_schedule_api() -> dict[str, Any]:
    """Check whether official schedule API responds (prefer bare domain)."""
    import httpx

    from app.cpbl_service import CPBL_BASE

    result: dict[str, Any] = {"base": CPBL_BASE, "ok": False, "status": None, "detail": ""}
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"},
        ) as client:
            page = await client.get(f"{CPBL_BASE}/schedule")
            result["schedulePage"] = page.status_code
            import re

            m = re.search(
                r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page.text
            )
            if not m:
                result["detail"] = "no csrf"
                return result
            token = m.group(1)
            today = date.today()
            cal = f"{today.year}/{today.month:02d}/{today.day:02d}"
            resp = await client.post(
                f"{CPBL_BASE}/schedule/getgamedatas",
                data={"calendar": cal, "location": "", "kindCode": "A"},
                headers={
                    "RequestVerificationToken": token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{CPBL_BASE}/schedule",
                    "Origin": CPBL_BASE,
                },
            )
            result["status"] = resp.status_code
            if resp.status_code != 200:
                result["detail"] = f"http {resp.status_code}"
                return result
            try:
                payload = resp.json()
            except Exception:
                result["detail"] = "non-json"
                return result
            games = payload if isinstance(payload, list) else payload.get("GameDatas") or []
            if isinstance(games, str):
                games = json.loads(games)
            result["ok"] = True
            result["gamesToday"] = len(games) if isinstance(games, list) else 0
            return result
    except Exception as exc:
        result["detail"] = str(exc)
        return result


async def repair_cpbl_pitchers() -> dict[str, Any]:
    """Compatibility wrapper — full live matchup repair (game + pitchers + lineups)."""
    from app.data_validate import repair_cpbl_cache_from_live

    return await repair_cpbl_cache_from_live(games=DEFAULT_GAMES)


async def _audit_cpbl_docs_vs_live(data_dir: Path) -> dict[str, Any]:
    from app.data_validate import (
        audit_cpbl_against_expected,
        expected_cpbl_matchups,
        unwrap_matchup_payload,
    )

    expected = await expected_cpbl_matchups()
    issues: list[str] = []
    warnings: list[str] = []
    rows = _load_matchups(data_dir, "cpbl")
    by_tid = {tid: data for tid, data in rows}
    for tid in range(1, 7):
        data = by_tid.get(tid) or {}
        if not data:
            # Also try reading from live cache if docs missing this team.
            try:
                from app.cpbl_cache import get_matchup, load_from_disk

                load_from_disk()
                data = unwrap_matchup_payload(get_matchup(tid, DEFAULT_GAMES))
            except Exception:
                data = {}
        if not data:
            issues.append(f"cpbl team {tid}: no docs/cache matchup for live compare")
            continue
        cross = audit_cpbl_against_expected(tid, data, expected.get(tid))
        issues.extend(cross["issues"])
        warnings.extend(cross["warnings"])
    return {"issues": issues, "warnings": warnings, "expected": expected}


async def run_audit(*, data_dir: Path, repair: bool) -> dict[str, Any]:
    api = await _probe_cpbl_schedule_api()
    cpbl = audit_league_files(data_dir, "cpbl", min_teams=4, min_games=5)
    npb = audit_league_files(data_dir, "npb", min_teams=8, min_games=5)
    mlb = audit_league_files(data_dir, "mlb", min_teams=20, min_games=5)

    live_cross = await _audit_cpbl_docs_vs_live(data_dir)
    cpbl["issues"] = list(cpbl.get("issues") or []) + list(live_cross.get("issues") or [])
    cpbl["warnings"] = list(cpbl.get("warnings") or []) + list(
        live_cross.get("warnings") or []
    )
    cpbl["liveCross"] = {
        "issueCount": len(live_cross.get("issues") or []),
        "warningCount": len(live_cross.get("warnings") or []),
    }

    repair_result = None
    needs_repair = repair and (
        cpbl["missingPitchers"] > 0
        or not api.get("ok")
        or bool(live_cross.get("issues"))
        or any("absurd" in i or "wrong game" in i or "wrong matchup" in i for i in cpbl["issues"])
    )
    if needs_repair:
        logger.warning(
            "CPBL needs repair (missingPitchers=%s liveIssues=%s api_ok=%s)",
            cpbl["missingPitchers"],
            len(live_cross.get("issues") or []),
            api.get("ok"),
        )
        repair_result = await repair_cpbl_pitchers()
        # Re-audit cache after repair
        from app.data_validate import validate_cpbl_cache

        cache_report = await validate_cpbl_cache(games=DEFAULT_GAMES, repair=False)
        repair_result["afterIssues"] = cache_report.get("issues") or []
        repair_result["afterOk"] = cache_report.get("ok")
        cpbl["missingPitchersAfterRepair"] = sum(
            1 for i in (cache_report.get("issues") or []) if "missing starter" in i
        )
        # Surface remaining live mismatches as critical
        for msg in cache_report.get("issues") or []:
            if msg not in cpbl["issues"]:
                cpbl["issues"].append(msg)

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "cpblApi": api,
        "cpbl": cpbl,
        "npb": npb,
        "mlb": mlb,
        "repair": repair_result,
    }

    critical = list(cpbl["issues"]) + list(npb["issues"]) + list(mlb["issues"])
    # Live schedule timing races (game ends mid-build) should not block Pages deploy.
    # Keep structural bugs (absurd avgs, H>AB, thin panels, duplicate lineup) as critical.
    soft_live = (
        "wrong gameSno",
        "wrong matchup date",
        "pitcher cached=",
        "pitcher missing",
        "no live schedule matchup",
        "no docs/cache matchup for live compare",
    )
    hard_critical = [
        msg
        for msg in critical
        if not any(token in msg for token in soft_live)
    ]
    live_soft = [msg for msg in critical if msg not in hard_critical]
    report["critical"] = hard_critical
    report["liveSoftIssues"] = live_soft
    report["ok"] = len(hard_critical) == 0 and bool(api.get("ok") or (cpbl["teams"] >= 4))
    if live_soft:
        logger.warning("Soft live-timing issues (do not fail deploy): %s", len(live_soft))
        for msg in live_soft[:12]:
            logger.warning("  live: %s", msg)

    out_path = data_dir / "health.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s (ok=%s critical=%s)", out_path, report["ok"], len(critical))
    for w in (cpbl.get("warnings") or [])[:30]:
        logger.warning("%s", w)
    for i in critical:
        logger.error("%s", i)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        default=str(ROOT / "docs" / "data"),
        help="Static data dir to audit (docs/data or data)",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Auto-repair CPBL wrong/missing matchups against live schedule",
    )
    parser.add_argument(
        "--fail-on-critical",
        action="store_true",
        help="Exit 1 on any critical validation issue",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Validate + repair local CPBL cache only (skip docs MLB/NPB files)",
    )
    args = parser.parse_args()
    if args.cache_only:
        from app.data_validate import validate_cpbl_cache

        report = asyncio.run(validate_cpbl_cache(games=DEFAULT_GAMES, repair=args.repair))
        out = ROOT / "data" / "health_cpbl_cache.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Wrote %s ok=%s issues=%s", out, report.get("ok"), len(report.get("issues") or []))
        if args.fail_on_critical and not report.get("ok"):
            raise SystemExit(1)
        return

    data_dir = Path(args.data_dir)
    report = asyncio.run(run_audit(data_dir=data_dir, repair=args.repair))
    if args.fail_on_critical and report.get("critical"):
        raise SystemExit(1)
    cpbl = report.get("cpbl") or {}
    after = cpbl.get("missingPitchersAfterRepair")
    if after is not None and after >= 4:
        logger.warning("CPBL starters still largely missing after repair (%s)", after)


if __name__ == "__main__":
    main()
