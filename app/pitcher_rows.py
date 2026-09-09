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


def pitcher_analysis_incomplete(
    analysis: dict[str, Any] | None,
    *,
    game_count: int = 10,
    expected_starts: int | None = None,
) -> bool:
    """True when cached pitcher starts look like a partial/stale fetch."""
    if not isinstance(analysis, dict):
        return True
    games = analysis.get("games") or []
    if not games:
        return True
    cached = len(games)
    pool_size = analysis.get("startPoolSize")
    if isinstance(pool_size, int) and pool_size > cached:
        return True
    if expected_starts is not None and expected_starts > cached:
        target = min(game_count, expected_starts)
        # Only flag when we clearly expect more rows (established starter mid-season).
        if target >= 3 and cached < target:
            return True
    return False


def pitcher_analysis_needs_rebuild(
    payload: dict[str, Any],
    *,
    game_count: int = 10,
    expected_starts_by_side: dict[str, int] | None = None,
) -> bool:
    """True when any side's pitcher block should be recomputed from box scores."""
    if pitcher_analysis_missing_pitch_counts(payload):
        return True
    expected = expected_starts_by_side or {}
    for side in ("away", "home"):
        panel = payload.get(side) or {}
        starter = ((panel.get("probablePitcher") or {}).get("fullName") or "").strip()
        if not starter:
            continue
        analysis = panel.get("pitcherAnalysis")
        if pitcher_analysis_incomplete(
            analysis,
            game_count=game_count,
            expected_starts=expected.get(side),
        ):
            return True
    return False


def cached_pitcher_analysis_complete(
    games_list: list[dict[str, Any]],
    *,
    game_count: int,
    expected_starts: int = 0,
) -> bool:
    """True when a cached pitcher block is safe to keep instead of re-fetching."""
    if not games_list:
        return False
    if expected_starts:
        return len(games_list) >= min(game_count, expected_starts)
    return len(games_list) >= game_count
