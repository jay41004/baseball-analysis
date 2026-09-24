"""GitHub Pages mirror freshness guards."""

from __future__ import annotations

import pytest

from app.pages_mirror import matchup_payload_stale, pages_slate_dates_match


def test_pages_slate_dates_match_today(monkeypatch):
    monkeypatch.setattr("app.slate_service._today_tomorrow", lambda: ("2026-09-13", "2026-09-14"))
    assert pages_slate_dates_match({"today": "2026-09-13", "tomorrow": "2026-09-14"})
    assert not pages_slate_dates_match({"today": "2026-09-09", "tomorrow": "2026-09-10"})


def test_matchup_payload_stale_old_header():
    data = {"matchup": {"date": "2026-09-09", "gamePk": 823092}}
    assert matchup_payload_stale(data, league="mlb")


def test_matchup_payload_stale_future_game():
    data = {"matchup": {"date": "2099-01-01", "gamePk": 824952}}
    assert not matchup_payload_stale(data, league="mlb")


@pytest.mark.asyncio
async def test_fetch_pages_slate_bucket_rejects_stale(monkeypatch):
    from app.pages_mirror import fetch_pages_slate_bucket

    async def fake_fetch(_path: str, **kwargs):
        return {
            "today": "2026-09-09",
            "tomorrow": "2026-09-10",
            "todayGames": [{"awayTeamId": 1, "homeTeamId": 2}],
            "tomorrowGames": [],
        }

    monkeypatch.setattr("app.pages_mirror.fetch_pages_json", fake_fetch)
    monkeypatch.setattr("app.slate_service._today_tomorrow", lambda: ("2026-09-13", "2026-09-14"))
    assert await fetch_pages_slate_bucket("mlb") is None
