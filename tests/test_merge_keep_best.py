"""merge_keep_best must never wholesale-revert a league directory."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.merge_keep_best import merge


def test_merge_never_replaces_whole_league(tmp_path: Path) -> None:
    prev = tmp_path / "prev"
    new = tmp_path / "new"
    prev_league = prev / "data" / "mlb"
    new_league = new / "data" / "mlb"
    prev_league.mkdir(parents=True)
    new_league.mkdir(parents=True)

    old_payload = {
        "matchup": {"date": "2026-09-10", "gamePk": 1},
        "away": {"teamId": 1, "games": [{"date": "2026-09-10"}] * 10},
        "home": {"teamId": 2, "games": [{"date": "2026-09-10"}] * 10},
    }
    fresh_payload = {
        "matchup": {"date": "2026-09-12", "gamePk": 2},
        "away": {"teamId": 3, "games": [{"date": "2026-09-12"}] * 10},
        "home": {"teamId": 4, "games": [{"date": "2026-09-12"}] * 10},
    }
    for i in range(25):
        (prev_league / f"matchup_{i}_10.json").write_text(
            json.dumps(old_payload), encoding="utf-8"
        )
    for i in range(5):
        (new_league / f"matchup_{i}_10.json").write_text(
            json.dumps(fresh_payload), encoding="utf-8"
        )

    merge(prev, new)

    dates = {
        json.loads(p.read_text(encoding="utf-8"))["matchup"]["date"]
        for p in new_league.glob("matchup_*_10.json")
    }
    assert "2026-09-12" in dates
    assert "2026-09-10" not in dates or len(dates) == 5
