"""Tests for extended cache audit rules."""

from __future__ import annotations

import unittest

from app.data_validate import (
    audit_matchup_data,
    team_ids_from_issues,
    _is_repairable_issue,
)


class DataValidateAuditTests(unittest.TestCase):
    def test_flags_missing_scored_pool(self) -> None:
        data = {
            "matchup": {"date": "2026-09-05", "status": "Scheduled"},
            "away": {"games": [{"isHome": False}] * 10},
            "home": {"games": [{"isHome": True}] * 10},
            "situational": {},
        }
        result = audit_matchup_data("mlb", 136, data, min_games=5)
        self.assertTrue(
            any("missing _scoredPool" in msg for msg in result["issues"]),
            result["issues"],
        )

    def test_flags_situational_game_count_mismatch(self) -> None:
        data = {
            "matchup": {"date": "2026-09-05", "status": "Scheduled"},
            "away": {
                "teamName": "運動家",
                "_scoredPool": [
                    {"isHome": False, "scoredInnings": [1]}
                    for _ in range(10)
                ],
            },
            "home": {
                "teamName": "水手",
                "_scoredPool": [
                    {"isHome": True, "scoredInnings": [2]}
                    for _ in range(10)
                ],
            },
            "situational": {
                "awayTeamAwayGames": {"gameCount": 4, "scoredCounts": {}},
                "homeTeamHomeGames": {"gameCount": 10, "scoredCounts": {}},
            },
        }
        result = audit_matchup_data("mlb", 136, data, min_games=5)
        self.assertTrue(
            any("situational gameCount awayTeamAwayGames" in msg for msg in result["issues"]),
            result["issues"],
        )

    def test_flags_confirmed_empty_lineup(self) -> None:
        data = {
            "matchup": {"date": "2026-09-05", "status": "Scheduled"},
            "away": {"games": [{}] * 10, "_scoredPool": [{}] * 10},
            "home": {"games": [{}] * 10, "_scoredPool": [{}] * 10},
            "startingLineups": {
                "away": {"source": "confirmed", "batters": []},
                "home": {"source": "pending", "batters": []},
            },
            "situational": {
                "awayTeamAwayGames": {"gameCount": 10},
                "homeTeamHomeGames": {"gameCount": 10},
            },
        }
        result = audit_matchup_data("mlb", 136, data, min_games=5)
        self.assertTrue(
            any("source=confirmed but empty" in msg for msg in result["issues"]),
            result["issues"],
        )

    def test_team_ids_from_issues(self) -> None:
        issues = [
            "mlb team 136: thin panels away=2 home=2",
            "mlb team 140: missing _scoredPool (games=10 pool=0)",
            "npb team 1: thin panels away=3 home=3",
        ]
        self.assertEqual(team_ids_from_issues(issues, "mlb"), [136, 140])
        self.assertEqual(team_ids_from_issues(issues, "npb"), [1])

    def test_repairable_issue_markers(self) -> None:
        self.assertTrue(_is_repairable_issue("mlb team 136: missing _scoredPool (games=10 pool=0)"))
        self.assertTrue(_is_repairable_issue("mlb team 136: wrong gamePk cached=1 expected=2"))
        self.assertFalse(_is_repairable_issue("mlb team 136: away Foo recent3Avg absurd"))

    def test_audit_mlb_wrong_game_pk(self) -> None:
        from app.data_validate import audit_mlb_against_expected

        data = {
            "matchup": {"date": "2026-09-08", "gamePk": 100, "status": "Scheduled"},
            "away": {"teamId": 136},
            "home": {"teamId": 140},
        }
        expected = {
            "date": "2026-09-07",
            "gamePk": 200,
            "status": "In Progress",
            "away": {"teamId": 136},
            "home": {"teamId": 140},
        }
        result = audit_mlb_against_expected(136, data, expected)
        self.assertTrue(
            any("wrong gamePk" in msg for msg in result["issues"]),
            result["issues"],
        )

    def test_pending_empty_lineup_not_stale_cross_game(self) -> None:
        """Empty pending lineups must not be flagged as cross-game bleed."""
        from app.matchup_integrity import strip_untrusted_lineups_inplace

        data = {
            "matchup": {"date": "2026-09-05", "status": "Scheduled"},
            "away": {"teamName": "運動家"},
            "home": {"teamName": "水手"},
            "startingLineups": {
                "logicVersion": 1,
                "away": {"source": "pending", "batters": []},
                "home": {"source": "pending", "batters": []},
            },
        }
        self.assertFalse(strip_untrusted_lineups_inplace(data, "mlb"))
        result = audit_matchup_data("mlb", 136, data, min_games=5)
        self.assertFalse(
            any("from another game" in msg for msg in result["issues"]),
            result["issues"],
        )


if __name__ == "__main__":
    unittest.main()
