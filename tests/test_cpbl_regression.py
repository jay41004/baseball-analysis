"""CPBL regression tests — run: python scripts/verify_cpbl.py"""

from __future__ import annotations

import unittest

from app.cpbl_verify import PITCHER_GOLDEN, SCHEDULE_MUST_INCLUDE, verify_cpbl


class CpblRegressionConfigTests(unittest.TestCase):
    def test_golden_cases_cover_known_regressions(self) -> None:
        snos = {case.game_sno for case in PITCHER_GOLDEN}
        self.assertIn(154, snos)
        self.assertIn(158, snos)
        self.assertIn(168, snos)

    def test_schedule_must_include_key_games(self) -> None:
        self.assertIn(168, SCHEDULE_MUST_INCLUDE)


class CpblLiveVerificationTests(unittest.TestCase):
    def test_cpbl_data_passes_regression_suite(self) -> None:
        import asyncio

        issues = asyncio.run(verify_cpbl(reset_schedule_cache=False))
        if issues:
            lines = "\n".join(f"[{item.check}] {item.detail}" for item in issues)
            self.fail(f"CPBL verification failed:\n{lines}")


if __name__ == "__main__":
    unittest.main()
