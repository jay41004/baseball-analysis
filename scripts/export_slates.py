"""Export today/tomorrow slates into docs/data for GitHub Pages."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _empty_slate(league: str) -> dict:
    from app.slate_service import _today_tomorrow

    today, tomorrow = _today_tomorrow()
    return {
        "league": league,
        "today": today,
        "tomorrow": tomorrow,
        "todayGames": [],
        "tomorrowGames": [],
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "error": True,
    }


async def _export_one(league: str) -> dict:
    from app.slate_service import fetch_league_slate

    return await fetch_league_slate(league)


def export_slates(data_dir: Path) -> None:
    """Write docs/data/{mlb,npb,cpbl}/slate.json for static game picker."""

    async def _run() -> None:
        for league in ("mlb", "npb", "cpbl"):
            out = data_dir / league / "slate.json"
            try:
                payload = await _export_one(league)
                _write_json(out, payload)
                print(
                    f"{league.upper()} slate: today={len(payload.get('todayGames') or [])} "
                    f"tomorrow={len(payload.get('tomorrowGames') or [])}"
                )
            except Exception as exc:
                print(f"{league.upper()} slate failed ({exc}); writing empty slate")
                _write_json(out, _empty_slate(league))

    asyncio.run(_run())
