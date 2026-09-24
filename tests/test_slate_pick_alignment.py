"""Pick alignment: selected team must match picked slate game."""

from __future__ import annotations

import pytest

from app.matchup_pick import ExpectedMatchup
from app.slate_service import align_expected_with_slate, expected_from_slate_row


@pytest.mark.parametrize(
    "team_id,expected,rows,want_pk",
    [
        (
            158,
            ExpectedMatchup(
                date="2026-09-12",
                away_id=143,
                home_id=144,
                game_pk=824873,
            ),
            [
                {
                    "gamePk": 824873,
                    "awayTeamId": 143,
                    "homeTeamId": 144,
                    "date": "2026-09-12",
                },
                {
                    "gamePk": 823736,
                    "awayTeamId": 113,
                    "homeTeamId": 158,
                    "date": "2026-09-12",
                },
            ],
            823736,
        ),
        (
            143,
            ExpectedMatchup(
                date="2026-09-12",
                away_id=143,
                home_id=144,
                game_pk=824873,
            ),
            [
                {
                    "gamePk": 824873,
                    "awayTeamId": 143,
                    "homeTeamId": 144,
                    "date": "2026-09-12",
                },
                {
                    "gamePk": 823736,
                    "awayTeamId": 113,
                    "homeTeamId": 158,
                    "date": "2026-09-12",
                },
            ],
            824873,
        ),
    ],
)
@pytest.mark.asyncio
async def test_align_expected_uses_team_game_not_stale_pick(
    team_id, expected, rows, want_pk, monkeypatch
):
    async def fake_slate():
        return {"today": rows, "tomorrow": []}

    monkeypatch.setattr("app.slate_service.fetch_mlb_slate", fake_slate)
    monkeypatch.setattr("app.slate_service._today_tomorrow", lambda: ("2026-09-12", "2026-09-13"))
    aligned = await align_expected_with_slate(team_id, expected)
    assert aligned is not None
    assert aligned.game_pk == want_pk


def test_expected_includes_team():
    pick = ExpectedMatchup(date="2026-09-12", away_id=113, home_id=158, game_pk=823736)
    assert pick.includes_team(158)
    assert pick.includes_team(113)
    assert not pick.includes_team(143)


@pytest.mark.asyncio
async def test_align_drops_stale_pages_pick(monkeypatch):
    async def fake_slate():
        return {
            "today": [
                {
                    "gamePk": 823092,
                    "awayTeamId": 140,
                    "homeTeamId": 136,
                    "date": "2026-09-09",
                }
            ],
            "tomorrow": [],
        }

    async def fake_next(_team_id: int):
        from app.matchup_pick import ExpectedMatchup

        return ExpectedMatchup(
            date="2026-09-18",
            away_id=136,
            home_id=115,
            game_pk=824303,
        )

    monkeypatch.setattr("app.slate_service.fetch_mlb_slate", fake_slate)
    monkeypatch.setattr("app.slate_service._today_tomorrow", lambda: ("2026-09-17", "2026-09-18"))
    monkeypatch.setattr("app.slate_service.expected_from_next_mlb_game", fake_next)

    stale = ExpectedMatchup(date="2026-09-09", away_id=140, home_id=136, game_pk=823092)
    aligned = await align_expected_with_slate(136, stale)
    assert aligned is not None
    assert aligned.game_pk == 824303
