"""Refresh NPB next-game headers for all teams."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from app.npb_cache import DEFAULT_GAMES, load_from_disk
    from app.npb_scheduler import refresh_matchup_header
    from app.npb_service import fetch_npb_teams

    load_from_disk()
    teams = await fetch_npb_teams()
    for team in teams:
        tid = team["id"]
        print(f"refresh team {tid}...")
        await refresh_matchup_header(tid, DEFAULT_GAMES)
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
