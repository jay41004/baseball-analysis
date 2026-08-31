"""Shared helpers for pitcher per-start rows."""

from __future__ import annotations

from typing import Any


def mlb_feed_is_final(feed: dict[str, Any] | None) -> bool:
    if not feed:
        return False
    status = (feed.get("gameData") or {}).get("status") or {}
    abstract = str(status.get("abstractGameState") or "").strip()
    if abstract == "Final":
        return True
    detailed = str(status.get("detailedState") or "").strip()
    return detailed in {"Final", "Game Over", "Completed Early"}


def pitch_count_from_stat(stat: dict[str, Any] | None) -> int | None:
    if not stat:
        return None
    for key in ("numberOfPitches", "pitchesThrown"):
        raw = stat.get(key)
        if raw is None or str(raw).strip() in {"", "-"}:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def pitch_count_from_cpbl_line(line: dict[str, Any] | None) -> int | None:
    if not line:
        return None
    for key in ("PitchCnt", "pitchCnt", "PitchBallCnt", "BallCnt", "pitchBallCnt", "ballCnt"):
        raw = line.get(key)
        if raw is None or str(raw).strip() in {"", "-"}:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def pitcher_analysis_missing_pitch_counts(payload: dict[str, Any]) -> bool:
    """True when cached pitcher start rows lack pitch counts."""
    for side in ("away", "home"):
        analysis = (payload.get(side) or {}).get("pitcherAnalysis")
        if not isinstance(analysis, dict):
            continue
        games = analysis.get("games") or []
        if not games:
            continue
        for game in games:
            if game.get("pitchCount") is None:
                return True
    return False
