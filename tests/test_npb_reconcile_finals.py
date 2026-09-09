"""NPB schedule reconciliation marks finished games as Final."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.npb_service import NpbClient, reconcile_schedule_finals


class NpbReconcileFinalsTests(unittest.TestCase):
    async def _reconcile(self, html: str, parsed: dict) -> dict:
        client = NpbClient()
        meta = {
            "date": "2026-09-05",
            "status": "Scheduled",
            "href": "/scores/2026/0905/b-m-22/",
        }
        schedule = [meta]
        with patch.object(client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_resp = mock_get.return_value
            mock_resp.raise_for_status = lambda: None
            mock_resp.text = html
            with patch.object(client, "_parse_game_page", return_value=parsed):
                await reconcile_schedule_finals(client, schedule)
        await client.close()
        return meta

    def test_marks_final_when_score_page_ended(self) -> None:
        async def run() -> None:
            meta = await self._reconcile(
                "試合終了",
                {
                    "awayInnings": [0, 1],
                    "homeInnings": [2, 0],
                },
            )
            self.assertEqual(meta["status"], "Final")
            self.assertEqual(meta["awayScore"], 1)
            self.assertEqual(meta["homeScore"], 2)

        import asyncio

        asyncio.run(run())

    def test_keeps_scheduled_when_not_ended(self) -> None:
        async def run() -> None:
            meta = await self._reconcile(
                "予定",
                {"awayInnings": [0], "homeInnings": [0]},
            )
            self.assertEqual(meta["status"], "Scheduled")

        import asyncio

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
