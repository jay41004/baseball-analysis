"""Inspect NPB game page for batting lineup structure."""
import asyncio
import json
from pathlib import Path

from bs4 import BeautifulSoup

from app.npb_service import NPB_BASE, NpbClient


async def main() -> None:
    client = NpbClient()
    try:
        schedule = await client.fetch_schedule()
        game = [g for g in schedule if g["status"] == "Final" and g.get("href")][-1]
        url = f"{NPB_BASE}{game['href']}"
        resp = await client._http.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = []
        for row in soup.select("table tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
            if len(cells) >= 2 and cells[0].startswith("【"):
                rows.append({"label": cells[0], "text": cells[1]})
        out = Path("data/npb_lineup_sample.json")
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {len(rows)} rows to {out}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
