"""MLB recent-game rows use Taiwan calendar date."""

from __future__ import annotations

import unittest

from app.mlb_display import mlb_game_row_date


class MlbTaiwanDateTests(unittest.TestCase):
    def test_night_game_shows_taiwan_date(self) -> None:
        game = {
            "officialDate": "2026-09-04",
            "gameDate": "2026-09-05T02:10:00Z",
        }
        self.assertEqual(mlb_game_row_date(game), "2026-09-05")

    def test_fallback_to_official_date(self) -> None:
        game = {"officialDate": "2026-09-04"}
        self.assertEqual(mlb_game_row_date(game), "2026-09-04")

    def test_mlb_slate_buckets_taiwan_columns_to_us_official(self) -> None:
        from app.slate_service import _mlb_day_bucket

        # 台灣今天 9/7 → 美國 9/6；台灣明天 9/8 → 美國 9/7
        self.assertEqual(_mlb_day_bucket("2026-09-06", "2026-09-07", "2026-09-08"), "today")
        self.assertEqual(_mlb_day_bucket("2026-09-07", "2026-09-07", "2026-09-08"), "tomorrow")
        self.assertIsNone(_mlb_day_bucket("2026-09-08", "2026-09-07", "2026-09-08"))


if __name__ == "__main__":
    unittest.main()
