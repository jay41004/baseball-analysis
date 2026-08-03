"""Inspect NPB play-by-play for batting order."""
import asyncio
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from app.npb_service import NPB_BASE, NpbClient


async def main() -> None:
    client = NpbClient()
    try:
        schedule = await client.fetch_schedule()
        game = [g for g in schedule if g["status"] == "Final" and g.get("href")][-1]
        href = game["href"]
        pbp = await client.fetch_playbyplay(href)
        if not pbp:
            print("no pbp")
            return
        soup = BeautifulSoup(pbp, "html.parser")
        samples = []
        for heading in soup.find_all("h5")[:6]:
            title = heading.get_text(strip=True)
            rows = []
            for element in heading.find_all_next(["tr", "h5"]):
                if element.name == "h5":
                    break
                text = element.get_text(" ", strip=True)
                if text:
                    rows.append(text)
            samples.append({"inning": title, "rows": rows[:8]})
        out = Path("data/npb_pbp_sample.json")
        out.write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {len(samples)} innings to {out}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
