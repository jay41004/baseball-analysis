import asyncio
import re

from app.cpbl_stats import _decode_escaped_json_array, _fetch_stats_game_html, fetch_stats_schedule

HITTERS_RE = re.compile(r'\\"hitters\\":\[(.*?)\]')


async def main() -> None:
    sched = await fetch_stats_schedule(2026)
    game = [g for g in sched if g.get("status") == "Final"][-1]
    html = await _fetch_stats_game_html(game["gameSno"], 2026)
    marker = f"2026-A-{game['gameSno']}"
    idx = html.find(marker)
    chunk = html[idx : idx + 500_000]
    block = HITTERS_RE.findall(chunk)[0]
    rows = _decode_escaped_json_array(block)
    row = rows[0]
    print("keys:", sorted(row.keys()))
    for key, value in sorted(row.items()):
        low = key.lower()
        if any(token in low for token in ("avg", "risp", "score", "runner", "tb", "hit")):
            print(key, "=", value)


if __name__ == "__main__":
    asyncio.run(main())
