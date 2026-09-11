"""Smoke check: lineup reference policy + optional live local API probe.

Runs in GitHub Actions and local 本機常駐 loop. Exit 1 on regression.
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _run_unit_smoke() -> None:
    modules = [
        "tests.test_lineup_reference_policy",
        "tests.test_mlb_matchup_integrity",
        "tests.test_mlb_taiwan_date",
        "tests.test_npb_header_stale",
    ]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in modules:
        suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


def _probe_local_lineup(base: str, team_id: int, games: int = 10) -> dict:
    import httpx

    url = f"{base.rstrip('/')}/api/mlb/lineup?team_id={team_id}&games={games}&force=true"
    with httpx.Client(timeout=90.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def _audit_static_lineup_gaps(data_dir: Path) -> list[str]:
    """Flag scheduled MLB exports with zero batters on both sides (likely regression)."""
    issues: list[str] = []
    mlb_dir = data_dir / "mlb"
    if not mlb_dir.is_dir():
        return issues
    for path in mlb_dir.glob("matchup_*_10.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        matchup = payload.get("matchup") or {}
        status = str(matchup.get("status") or "").strip().lower()
        if status not in {"scheduled", "preview", "pre-game", "pregame", "warmup", ""}:
            continue
        lineups = payload.get("startingLineups") or {}
        away_n = len((lineups.get("away") or {}).get("batters") or [])
        home_n = len((lineups.get("home") or {}).get("batters") or [])
        if away_n == 0 and home_n == 0:
            tid = path.name.split("_")[1]
            issues.append(
                f"mlb team {tid}: scheduled {matchup.get('date')} has empty lineups "
                f"(expected previous reference or pending)"
            )
    return issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "docs" / "data"))
    parser.add_argument("--local-base", default="", help="e.g. http://127.0.0.1:8000")
    parser.add_argument("--fail-on-static-gaps", action="store_true")
    parser.add_argument("--out", default="", help="Write JSON report path")
    args = parser.parse_args()

    report: dict = {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "unitTests": "pending",
        "staticGaps": [],
        "localProbe": None,
        "ok": True,
    }

    try:
        _run_unit_smoke()
        report["unitTests"] = "ok"
    except SystemExit:
        report["unitTests"] = "failed"
        report["ok"] = False

    gaps = _audit_static_lineup_gaps(Path(args.data_dir))
    report["staticGaps"] = gaps
    if args.fail_on_static_gaps and gaps:
        report["ok"] = False

    if args.local_base:
        try:
            sample = _probe_local_lineup(args.local_base, team_id=143)
            away = len((sample.get("away") or {}).get("batters") or [])
            home = len((sample.get("home") or {}).get("batters") or [])
            report["localProbe"] = {
                "teamId": 143,
                "awayBatters": away,
                "homeBatters": home,
                "awaySource": (sample.get("away") or {}).get("source"),
            }
            if away == 0 and home == 0:
                report["ok"] = False
                report["localProbe"]["error"] = "both sides empty after force fetch"
        except Exception as exc:
            report["localProbe"] = {"error": str(exc)}
            report["ok"] = False

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
