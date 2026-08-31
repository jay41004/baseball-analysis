"""Rebuild NPB matchup cache so pitcher rows include pitch counts."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from app.npb_cache import DEFAULT_GAMES, load_from_disk
    from app.npb_scheduler import refresh_matchup

    load_from_disk()
    for team_id in range(1, 13):
        print(f"refresh team {team_id}...")
        await refresh_matchup(team_id, DEFAULT_GAMES)
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
