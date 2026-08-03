import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cpbl_probable import enrich_schedule_from_ptt
import httpx


async def main() -> None:
    schedule = json.loads((ROOT / "data/cpbl_schedule.json").read_text(encoding="utf-8"))
    games = [g for g in schedule["games"] if g.get("date") == "2026-07-29"]
    async with httpx.AsyncClient(timeout=60) as http:
        await enrich_schedule_from_ptt(http, games)
    (ROOT / "_today_ptt.json").write_text(json.dumps(games, ensure_ascii=False, indent=2), encoding="utf-8")


asyncio.run(main())
