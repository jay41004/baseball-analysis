"""NPB header staleness and live pitcher patch rules."""

from __future__ import annotations

import unittest

from app.matchup_pick import ExpectedMatchup
from app.npb_scheduler import npb_header_stale
from app.pitcher_peer_sync import patch_probable_pitcher_header


class NpbHeaderStaleTests(unittest.TestCase):
    def test_past_game_date_is_stale(self) -> None:
        data = {"matchup": {"date": "2026-09-08"}, "away": {}, "home": {}}
        self.assertTrue(npb_header_stale(data, None))

    def test_pick_mismatch_is_stale(self) -> None:
        data = {
            "matchup": {"date": "2026-09-09"},
            "away": {"teamId": 1},
            "home": {"teamId": 2},
        }
        expected = ExpectedMatchup(date="2026-09-10", away_id=1, home_id=2)
        self.assertTrue(npb_header_stale(data, expected))

    def test_force_refresh_updates_same_day_pitcher(self) -> None:
        panel = {"probablePitcher": {"fullName": "舊投手"}}
        changed = patch_probable_pitcher_header(
            panel,
            {"fullName": "新投手"},
            game_changed=False,
            force_refresh=True,
        )
        self.assertTrue(changed)
        self.assertEqual(panel["probablePitcher"]["fullName"], "新投手")


if __name__ == "__main__":
    unittest.main()
