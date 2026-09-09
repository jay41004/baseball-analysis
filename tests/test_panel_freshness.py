"""Tests for recent-game panel staleness detection."""

from __future__ import annotations

import unittest

from app.panel_freshness import latest_game_date_in_data, panel_staleness_issue


class PanelFreshnessTests(unittest.TestCase):
    def test_latest_game_date_in_data(self) -> None:
        data = {
            "away": {"games": [{"date": "2026-09-02"}, {"date": "2026-09-04"}]},
            "home": {"games": [{"date": "2026-09-03"}]},
        }
        self.assertEqual(latest_game_date_in_data(data), "2026-09-04")

    def test_panel_staleness_issue_when_behind(self) -> None:
        data = {
            "away": {"games": [{"date": "2026-09-02"}]},
            "home": {"games": [{"date": "2026-09-02"}]},
        }
        msg = panel_staleness_issue("mlb", 136, data, "2026-09-05")
        self.assertIn("stale", msg or "")

    def test_panel_staleness_issue_when_current(self) -> None:
        data = {
            "away": {"games": [{"date": "2026-09-05"}]},
            "home": {"games": [{"date": "2026-09-05"}]},
        }
        self.assertIsNone(panel_staleness_issue("mlb", 136, data, "2026-09-05"))


if __name__ == "__main__":
    unittest.main()
