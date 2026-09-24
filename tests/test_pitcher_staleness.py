"""Pitcher per-start row staleness detection."""

from __future__ import annotations

from app.panel_freshness import latest_game_date_in_pitcher_analysis
from app.pitcher_rows import (
    pitcher_analysis_incomplete,
    pitcher_analysis_needs_rebuild,
    pitcher_starter_mismatch,
)


def test_pitcher_starter_mismatch_detects_renamed_starter():
    data = {
        "away": {
            "probablePitcher": {"fullName": "Bryan Woo"},
            "pitcherAnalysis": {"pitcherName": "Bryce Miller", "games": [{"date": "2026-09-10"}]},
        }
    }
    assert pitcher_starter_mismatch(data)


def test_latest_game_date_in_pitcher_analysis():
    analysis = {
        "games": [
            {"date": "2026-09-08"},
            {"date": "2026-09-15"},
            {"date": "2026-09-12"},
        ]
    }
    assert latest_game_date_in_pitcher_analysis(analysis) == "2026-09-15"


def test_eight_starts_without_pool_flags_incomplete():
    analysis = {
        "pitcherName": "Test Pitcher",
        "games": [{"date": f"2026-09-{d:02d}", "pitchCount": 80} for d in range(1, 9)],
    }
    assert pitcher_analysis_incomplete(analysis, game_count=10)


def test_eight_starts_when_schedule_expects_twelve():
    analysis = {
        "pitcherName": "Test Pitcher",
        "games": [{"date": f"2026-09-{d:02d}", "pitchCount": 80} for d in range(1, 9)],
        "startPoolSize": 12,
    }
    assert pitcher_analysis_needs_rebuild(
        {
            "away": {
                "probablePitcher": {"fullName": "Test Pitcher"},
                "pitcherAnalysis": analysis,
            }
        },
        game_count=10,
        expected_starts_by_side={"away": 12},
    )
