"""Regression: scheduled games must keep previous-start reference lineups."""

from __future__ import annotations

import unittest

from app.matchup_integrity import guard_lineups_for_api, lineups_trusted_for_league
from app.mlb_service import LINEUP_LOGIC_VERSION, lineups_trusted_for_matchup


def _previous_card(team_name: str, source_date: str) -> dict:
    return {
        "teamName": team_name,
        "source": "previous",
        "sourceDate": source_date,
        "batters": [{"order": i, "name": f"P{i}"} for i in range(1, 10)],
    }


class LineupReferencePolicyTests(unittest.TestCase):
    def test_mlb_trusts_previous_from_earlier_date_on_scheduled(self) -> None:
        lineups = {
            "logicVersion": LINEUP_LOGIC_VERSION,
            "away": _previous_card("費城費城人", "2026-09-10"),
            "home": _previous_card("亞特蘭大勇士", "2026-09-10"),
        }
        self.assertTrue(
            lineups_trusted_for_matchup(
                lineups,
                matchup_date="2026-09-12",
                matchup_status="Scheduled",
            )
        )

    def test_guard_api_keeps_previous_reference_for_tonight(self) -> None:
        payload = {
            "matchup": {"date": "2026-09-12", "status": "Scheduled"},
            "away": {"teamName": "費城費城人"},
            "home": {"teamName": "亞特蘭大勇士"},
            "startingLineups": {
                "logicVersion": LINEUP_LOGIC_VERSION,
                "away": _previous_card("費城費城人", "2026-09-10"),
                "home": _previous_card("亞特蘭大勇士", "2026-09-10"),
            },
        }
        out = guard_lineups_for_api(payload, "mlb")
        self.assertEqual(len(out["away"]["batters"]), 9)
        self.assertEqual(out["away"]["source"], "previous")

    def test_league_helper_matches_mlb_previous_policy(self) -> None:
        lineups = {
            "logicVersion": LINEUP_LOGIC_VERSION,
            "away": _previous_card("運動家", "2026-09-01"),
            "home": _previous_card("水手", "2026-09-01"),
        }
        self.assertTrue(
            lineups_trusted_for_league(
                lineups,
                "mlb",
                matchup_date="2026-09-05",
                matchup_status="Scheduled",
            )
        )


if __name__ == "__main__":
    unittest.main()
