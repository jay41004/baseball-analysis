"""Guards against stale probable pitchers and cross-game lineup bleed."""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


def lineups_trusted_for_league(
    lineups: dict[str, Any] | None,
    league: str,
    *,
    matchup_date: str | None = None,
    matchup_status: str | None = None,
) -> bool:
    if not lineups:
        return False
    mdate = (matchup_date or "")[:10]
    status = (matchup_status or "").strip()
    if league == "mlb":
        from app.mlb_service import lineups_trusted_for_matchup

        return lineups_trusted_for_matchup(
            lineups, matchup_date=mdate, matchup_status=status
        )
    if league == "cpbl":
        from app.cpbl_service import cpbl_lineups_need_rebuild

        return not cpbl_lineups_need_rebuild(
            lineups, matchup_date=mdate, matchup_status=status
        )
    if league == "npb":
        from app.npb_service import npb_lineups_need_rebuild

        return not npb_lineups_need_rebuild(
            lineups, matchup_date=mdate, matchup_status=status
        )
    return True


def _blank_lineups(data: dict[str, Any], league: str) -> dict[str, Any]:
    matchup = data.get("matchup") or {}
    mdate = (matchup.get("date") or "")[:10]
    away = data.get("away") or {}
    home = data.get("home") or {}
    payload: dict[str, Any] = {
        "away": {
            "teamName": away.get("teamName") or "",
            "batters": [],
            "source": "pending",
            "sourceDate": mdate or None,
        },
        "home": {
            "teamName": home.get("teamName") or "",
            "batters": [],
            "source": "pending",
            "sourceDate": mdate or None,
        },
    }
    if league == "mlb":
        from app.mlb_service import LINEUP_LOGIC_VERSION

        payload["logicVersion"] = LINEUP_LOGIC_VERSION
    elif league == "cpbl":
        from app.cpbl_service import CPBL_LINEUP_LOGIC_VERSION

        payload["logicVersion"] = CPBL_LINEUP_LOGIC_VERSION
    elif league == "npb":
        from app.npb_service import NPB_LINEUP_LOGIC_VERSION

        payload["logicVersion"] = NPB_LINEUP_LOGIC_VERSION
    return payload


def strip_untrusted_lineups_inplace(data: dict[str, Any], league: str) -> bool:
    """Remove startingLineups from another game. Returns True if stripped."""
    lineups = data.get("startingLineups")
    if not lineups:
        return False
    has_card = any(
        len((lineups.get(side) or {}).get("batters") or []) >= 7 for side in ("away", "home")
    )
    if not has_card:
        # Not published yet — not cross-game bleed.
        return False
    matchup = data.get("matchup") or {}
    if lineups_trusted_for_league(
        lineups,
        league,
        matchup_date=(matchup.get("date") or "")[:10],
        matchup_status=matchup.get("status"),
    ):
        return False
    data["startingLineups"] = _blank_lineups(data, league)
    logger.info(
        "Stripped untrusted %s lineups for game %s",
        league,
        matchup.get("gamePk") or matchup.get("gameSno") or matchup.get("date"),
    )
    return True


def blank_lineups_for_matchup(data: dict[str, Any], league: str) -> dict[str, Any]:
    """Empty lineup card for the matchup header in *data* (wrong game / still loading)."""
    return _blank_lineups(data, league)


def sanitize_matchup_for_store(data: dict[str, Any], league: str) -> dict[str, Any]:
    """Last gate before writing cache to disk."""
    cleaned = copy.deepcopy(data)
    strip_untrusted_lineups_inplace(cleaned, league)
    return cleaned


def export_matchup_for_api(data: dict[str, Any], league: str) -> dict[str, Any]:
    """Strip internal pools and untrusted lineups before sending to the browser."""
    from app.inning_comparison import strip_panel_internals

    guarded = copy.deepcopy(data)
    strip_untrusted_lineups_inplace(guarded, league)
    for side in ("away", "home"):
        panel = guarded.get(side)
        if isinstance(panel, dict):
            guarded[side] = strip_panel_internals(panel)
    return guarded


def guard_matchup_for_api(data: dict[str, Any], league: str) -> dict[str, Any]:
    """Never send another game's lineup card to the browser."""
    return export_matchup_for_api(data, league)


def repair_league_store(store: dict[str, Any], *, league: str, key_prefix: str) -> int:
    """Scan in-memory cache on boot; strip stale lineups. Returns entries fixed."""
    fixed = 0
    for key, entry in list(store.items()):
        if not key.startswith(key_prefix):
            continue
        data = (entry or {}).get("data")
        if not isinstance(data, dict):
            continue
        if strip_untrusted_lineups_inplace(data, league):
            fixed += 1
    return fixed
