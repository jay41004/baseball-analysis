"""Refresh league caches then rebuild docs/ for GitHub Pages.

Designed for GitHub Actions (enough RAM for full MLB rebuilds).
Render free tier cannot run this safely.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refresh_static")

DEFAULT_GAMES = 10


async def _refresh_mlb(concurrency: int) -> None:
    from app.cache import load_from_disk
    from app.mlb_service import fetch_teams
    from app.scheduler import refresh_matchup

    load_from_disk()
    teams = await fetch_teams()
    sem = asyncio.Semaphore(concurrency)
    failed = 0

    async def one(team: dict) -> None:
        nonlocal failed
        async with sem:
            tid = int(team["id"])
            try:
                logger.info("MLB refresh team %s (%s)", tid, team.get("nameZh") or team.get("name"))
                await refresh_matchup(tid, DEFAULT_GAMES)
            except Exception:
                failed += 1
                logger.exception("MLB team %s failed (continuing)", tid)

    await asyncio.gather(*[one(t) for t in teams])
    logger.info("MLB refresh done (%s teams, %s failed)", len(teams), failed)
    if failed >= max(10, len(teams) // 2):
        raise RuntimeError(f"MLB refresh too many failures: {failed}/{len(teams)}")


async def _refresh_npb(concurrency: int) -> None:
    from app.npb_cache import load_from_disk
    from app.npb_scheduler import refresh_matchup
    from app.npb_service import fetch_npb_teams

    load_from_disk()
    teams = await fetch_npb_teams()
    sem = asyncio.Semaphore(concurrency)
    failed = 0

    async def one(team: dict) -> None:
        nonlocal failed
        async with sem:
            tid = int(team["id"])
            try:
                logger.info("NPB refresh team %s", tid)
                await refresh_matchup(tid, DEFAULT_GAMES)
            except Exception:
                failed += 1
                logger.exception("NPB team %s failed (continuing)", tid)

    await asyncio.gather(*[one(t) for t in teams])
    logger.info("NPB refresh done (%s teams, %s failed)", len(teams), failed)
    if failed >= max(6, len(teams) // 2):
        raise RuntimeError(f"NPB refresh too many failures: {failed}/{len(teams)}")


async def _refresh_cpbl(concurrency: int) -> None:
    from app.cpbl_cache import load_from_disk
    from app.cpbl_scheduler import refresh_matchup
    from app.cpbl_service import fetch_cpbl_teams

    load_from_disk()
    teams = await fetch_cpbl_teams()
    sem = asyncio.Semaphore(max(1, concurrency // 2))
    failed = 0

    async def one(team: dict) -> None:
        nonlocal failed
        async with sem:
            tid = int(team["id"])
            try:
                logger.info("CPBL refresh team %s", tid)
                await refresh_matchup(tid, DEFAULT_GAMES)
            except Exception:
                failed += 1
                logger.exception("CPBL team %s failed (continuing)", tid)

    await asyncio.gather(*[one(t) for t in teams])
    logger.info("CPBL refresh done (%s teams, %s failed)", len(teams), failed)
    if failed >= 4:
        raise RuntimeError(f"CPBL refresh too many failures: {failed}/{len(teams)}")


async def refresh_leagues(leagues: list[str], concurrency: int) -> None:
    # Force full rebuild path (not Render cloud-lite header-only).
    os.environ.pop("RENDER", None)
    os.environ["CLOUD_LITE"] = "0"

    errors: list[str] = []
    if "mlb" in leagues:
        try:
            await _refresh_mlb(concurrency)
        except Exception:
            logger.exception("MLB refresh failed")
            errors.append("mlb")
    if "npb" in leagues:
        try:
            await _refresh_npb(concurrency)
        except Exception:
            logger.exception("NPB refresh failed")
            errors.append("npb")
    if "cpbl" in leagues:
        try:
            await _refresh_cpbl(max(1, concurrency // 2))
        except Exception:
            logger.exception("CPBL refresh failed")
            errors.append("cpbl")

    # Never deploy a totally empty rebuild: MLB is required.
    if "mlb" in errors:
        raise SystemExit(f"Critical refresh failed: {', '.join(errors)}")
    if errors:
        logger.warning("Some leagues failed (site will still deploy with prior cache where possible): %s", ", ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--leagues",
        default=os.environ.get("REFRESH_LEAGUES", "mlb,npb,cpbl"),
        help="Comma list: mlb,npb,cpbl",
    )
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("REFRESH_CONCURRENCY", "2")))
    parser.add_argument("--skip-refresh", action="store_true", help="Only rebuild docs from existing cache")
    args = parser.parse_args()

    leagues = [x.strip().lower() for x in args.leagues.split(",") if x.strip()]
    if not args.skip_refresh:
        asyncio.run(refresh_leagues(leagues, args.concurrency))

    import importlib.util

    build_path = ROOT / "scripts" / "build_static_site.py"
    spec = importlib.util.spec_from_file_location("build_static_site", build_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.main()
    logger.info("Static site refresh complete")


if __name__ == "__main__":
    main()
