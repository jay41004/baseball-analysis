import asyncio
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cpbl_official import fetch_home_games_for_date
import httpx


async def main() -> None:
    today = date.today()
    async with httpx.AsyncClient(timeout=60, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }) as http:
        games = await fetch_home_games_for_date(http, today)
    rows = []
    for g in games or []:
        rows.append(
            {
                "date": g.get("date") or today.isoformat(),
                "away": g.get("awayNameZh"),
                "home": g.get("homeNameZh"),
                "away_pitcher": g.get("awayProbablePitcher") or "尚未公布",
                "home_pitcher": g.get("homeProbablePitcher") or "尚未公布",
                "status": g.get("status"),
            }
        )
    out = ROOT / "_cpbl_official_today.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(len(rows))


asyncio.run(main())
