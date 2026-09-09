"""One-click local health check: unit tests + cache validation.

Run:
  PYTHONPATH=. python scripts/run_health_check.py
  check.bat
  check.bat --repair
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")


def run_unit_tests() -> int:
    print("=== Unit tests (offline) ===", flush=True)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_mlb_matchup_integrity",
            "tests.test_data_validate_audit",
            "tests.test_panel_freshness",
            "tests.test_cpbl_regression.CpblRegressionConfigTests",
            "-q",
        ],
        cwd=ROOT,
        env={**dict(**__import__("os").environ), "PYTHONPATH": str(ROOT)},
    )
    if proc.returncode == 0:
        print("Unit tests: OK", flush=True)
    else:
        print(f"Unit tests: FAILED (exit {proc.returncode})", flush=True)
    return proc.returncode


async def run_cache_validation(
    *,
    repair: bool,
    games: int,
    online: bool,
) -> tuple[int, dict]:
    from app.data_validate import validate_all_caches

    print("\n=== Cache validation ===", flush=True)
    report = await validate_all_caches(
        repair_cpbl=repair,
        repair_mlb=repair,
        repair_npb=repair,
        games=games,
        offline=not online,
    )
    for league in ("mlb", "npb", "cpbl"):
        block = report.get(league) or {}
        issues = block.get("issues") or []
        warnings = block.get("warnings") or []
        print(
            f"{league.upper()}: ok={block.get('ok')} "
            f"issues={len(issues)} warnings={len(warnings)}",
            flush=True,
        )
        for msg in issues[:8]:
            print(f"  ! {msg}", flush=True)
        if len(issues) > 8:
            print(f"  ... +{len(issues) - 8} more", flush=True)

    out = ROOT / "data" / "health_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    return (0 if report.get("ok") else 1), report


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run unit tests + cache validation")
    parser.add_argument("--no-tests", action="store_true", help="Skip unittest")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Auto-repair MLB/NPB/CPBL when validation fails",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="CPBL cross-check against live schedule (slower, needs network)",
    )
    parser.add_argument("--games", type=int, default=10)
    args = parser.parse_args()

    code = 0
    if not args.no_tests:
        code = run_unit_tests()
        if code != 0:
            return code

    val_code, _ = await run_cache_validation(
        repair=args.repair,
        games=args.games,
        online=args.online,
    )
    return val_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
