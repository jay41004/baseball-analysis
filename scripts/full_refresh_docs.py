"""Export fresh docs/ from local cache (optionally refresh NPB headers first)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def refresh_npb_headers() -> None:
    from app.npb_cache import DEFAULT_GAMES, load_from_disk
    from app.npb_scheduler import refresh_matchup_header
    from app.npb_service import fetch_npb_teams

    load_from_disk()
    teams = await fetch_npb_teams()
    sem = asyncio.Semaphore(3)

    async def one(team_id: int) -> None:
        async with sem:
            print(f"NPB header refresh team {team_id}...")
            await refresh_matchup_header(team_id, DEFAULT_GAMES)

    await asyncio.gather(*(one(t["id"]) for t in teams))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--headers-only",
        action="store_true",
        help="Only refresh NPB next-game headers (fast), skip full matchup rebuild",
    )
    args = parser.parse_args()

    if args.headers_only:
        asyncio.run(refresh_npb_headers())

    import importlib.util

    path = ROOT / "scripts" / "build_static_site.py"
    spec = importlib.util.spec_from_file_location("build_static_site", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.main()


if __name__ == "__main__":
    main()
