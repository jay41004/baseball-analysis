"""Check CPBL probable/starting pitchers for upcoming games."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cpbl_service import CpblClient, fetch_cpbl_teams, fetch_next_matchup


async def main() -> None:
    client = CpblClient()
    teams = await fetch_cpbl_teams()
    today = date.today().isoformat()
    print(f"TODAY={today}")
    seen: set[str] = set()
    rows = []
    for team in teams:
        matchup = await fetch_next_matchup(client, team["id"])
        if not matchup:
            continue
        away = matchup.get("away") or {}
        home = matchup.get("home") or {}
        label = f"{away.get('nameZh', '?')} @ {home.get('nameZh', '?')}"
        if label in seen:
            continue
        seen.add(label)
        gd = str(matchup.get("gameDate") or matchup.get("date") or "")[:10]
        away_p = (away.get("probablePitcher") or {}).get("fullName") or "尚未公布"
        home_p = (home.get("probablePitcher") or {}).get("fullName") or "尚未公布"
        rows.append(
            {
                "date": gd,
                "matchup": label,
                "away_pitcher": away_p,
                "home_pitcher": home_p,
                "status": matchup.get("status"),
            }
        )
    await client.close()
    for row in sorted(rows, key=lambda r: r["date"]):
        print(json.dumps(row, ensure_ascii=False))
    today_rows = [r for r in rows if r["date"] == today]
    out = ROOT / "_cpbl_today.json"
    out.write_text(json.dumps(today_rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
