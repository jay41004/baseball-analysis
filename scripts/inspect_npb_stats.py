"""Search NPB game HTML for batting stats."""
import asyncio
import re
from pathlib import Path

from app.npb_service import NPB_BASE, NpbClient


async def main() -> None:
    client = NpbClient()
    try:
        schedule = await client.fetch_schedule()
        game = [g for g in schedule if g["status"] == "Final" and g.get("href")][-1]
        url = f"{NPB_BASE}{game['href']}"
        resp = await client._http.get(url)
        html = resp.text
        Path("data/npb_game_snippet.html").write_text(html[:120000], encoding="utf-8")
        for pattern in ["打率", "本塁打", "打点", "スタメン", "打順", "batting"]:
            print(pattern, html.count(pattern))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
