"""Regression tests for matchup integrity guards."""

from __future__ import annotations

import unittest

from app.matchup_integrity import (
    guard_matchup_for_api,
    lineups_trusted_for_league,
    sanitize_matchup_for_store,
    strip_untrusted_lineups_inplace,
)
from app.mlb_service import LINEUP_LOGIC_VERSION
from app.pitcher_peer_sync import (
    merge_probable_pitchers_from_cache,
    patch_probable_pitcher_header,
)


class MatchupIntegrityTests(unittest.TestCase):
    def test_keeps_previous_lineups_for_scheduled_game(self) -> None:
        data = {
            "matchup": {"date": "2026-09-05", "gamePk": 1, "status": "Scheduled"},
            "away": {"teamName": "運動家"},
            "home": {"teamName": "水手"},
            "startingLineups": {
                "logicVersion": LINEUP_LOGIC_VERSION,
                "away": {
                    "teamName": "運動家",
                    "source": "previous",
                    "sourceDate": "2026-09-02",
                    "batters": [{"order": i, "name": "A"} for i in range(1, 10)],
                },
                "home": {
                    "teamName": "水手",
                    "source": "previous",
                    "sourceDate": "2026-09-02",
                    "batters": [{"order": i, "name": "B"} for i in range(1, 10)],
                },
            },
        }
        self.assertFalse(strip_untrusted_lineups_inplace(data, "mlb"))
        self.assertEqual(len(data["startingLineups"]["away"]["batters"]), 9)

    def test_guard_api_keeps_previous_reference_on_scheduled(self) -> None:
        payload = {
            "matchup": {"date": "2026-09-05", "status": "Scheduled"},
            "away": {"teamName": "運動家"},
            "home": {"teamName": "水手"},
            "startingLineups": {
                "logicVersion": LINEUP_LOGIC_VERSION,
                "away": {
                    "source": "previous",
                    "sourceDate": "2026-09-02",
                    "batters": [{"order": i} for i in range(1, 10)],
                },
                "home": {
                    "source": "previous",
                    "sourceDate": "2026-09-02",
                    "batters": [{"order": i} for i in range(1, 10)],
                },
            },
        }
        out = guard_matchup_for_api(payload, "mlb")
        self.assertEqual(len(out["startingLineups"]["away"]["batters"]), 9)
        self.assertEqual(len(out["startingLineups"]["home"]["batters"]), 9)

    def test_confirmed_same_day_lineups_are_trusted(self) -> None:
        lineups = {
            "logicVersion": LINEUP_LOGIC_VERSION,
            "away": {
                "source": "confirmed",
                "sourceDate": "2026-09-04",
                "batters": [{"order": i} for i in range(1, 10)],
            },
            "home": {
                "source": "confirmed",
                "sourceDate": "2026-09-04",
                "batters": [{"order": i} for i in range(1, 10)],
            },
        }
        self.assertTrue(
            lineups_trusted_for_league(
                lineups,
                "mlb",
                matchup_date="2026-09-04",
                matchup_status="In Progress",
            )
        )

    def test_sanitize_on_store_clears_stale_lineups(self) -> None:
        raw = {
            "matchup": {"date": "2026-09-05", "status": "Scheduled"},
            "away": {"teamName": "運動家"},
            "home": {"teamName": "水手"},
            "startingLineups": {
                "logicVersion": LINEUP_LOGIC_VERSION,
                "away": {
                    "source": "previous",
                    "sourceDate": "2026-09-02",
                    "batters": [{"order": i} for i in range(1, 10)],
                },
                "home": {"batters": []},
            },
        }
        cleaned = sanitize_matchup_for_store(raw, "mlb")
        self.assertEqual(cleaned["startingLineups"]["away"]["batters"], [])

    def test_refresh_situational_counts_home_away_splits(self) -> None:
        from app.inning_comparison import build_matchup_situational

        away_panel = {
            "teamName": "運動家",
            "_scoredPool": [
                {"isHome": False, "scoredInnings": [1]},
                {"isHome": True, "scoredInnings": [2]},
            ],
        }
        home_panel = {
            "teamName": "水手",
            "_scoredPool": [
                {"isHome": True, "scoredInnings": [3]},
                {"isHome": False, "scoredInnings": [4]},
            ],
        }
        sit = build_matchup_situational(away_panel, home_panel)
        self.assertEqual(sit["awayTeamAwayGames"]["gameCount"], 1)
        self.assertEqual(sit["homeTeamHomeGames"]["gameCount"], 1)

    def test_empty_lineups_are_not_stripped_as_untrusted(self) -> None:
        data = {
            "matchup": {"date": "2026-09-05", "status": "Scheduled"},
            "away": {"teamName": "運動家"},
            "home": {"teamName": "水手"},
            "startingLineups": {
                "logicVersion": LINEUP_LOGIC_VERSION,
                "away": {"batters": [], "source": "pending"},
                "home": {"batters": [], "source": "pending"},
            },
        }
        self.assertFalse(strip_untrusted_lineups_inplace(data, "mlb"))
        self.assertEqual(data["startingLineups"]["away"]["source"], "pending")


class ProbablePitcherGuardTests(unittest.TestCase):
    def test_same_game_header_does_not_replace_known_starter(self) -> None:
        panel = {"probablePitcher": {"fullName": "Jack Perkins", "id": 1}}
        patch_probable_pitcher_header(
            panel, {"fullName": "Logan Gilbert", "id": 2}, game_changed=False
        )
        self.assertEqual(panel["probablePitcher"]["fullName"], "Jack Perkins")

    def test_merge_keeps_cached_starter_when_api_regresses(self) -> None:
        new = {
            "matchup": {"gamePk": 99},
            "away": {
                "teamId": 133,
                "probablePitcher": {"fullName": "Logan Gilbert"},
            },
            "home": {"teamId": 136, "probablePitcher": None},
        }
        old = {
            "matchup": {"gamePk": 99},
            "away": {
                "teamId": 133,
                "probablePitcher": {"fullName": "Jack Perkins", "id": 1},
            },
            "home": {
                "teamId": 136,
                "probablePitcher": {"fullName": "Kade Anderson", "id": 2},
            },
        }
        merge_probable_pitchers_from_cache(new, old, league="mlb", fill_only=False)
        self.assertEqual(new["away"]["probablePitcher"]["fullName"], "Jack Perkins")
        self.assertEqual(new["home"]["probablePitcher"]["fullName"], "Kade Anderson")


if __name__ == "__main__":
    unittest.main()
