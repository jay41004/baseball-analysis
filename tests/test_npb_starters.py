"""NPB battery / linescore parse regressions."""

from pathlib import Path

from bs4 import BeautifulSoup

from app.npb_service import (
    NpbClient,
    _starter_from_battery,
    _trim_linescore_rhe,
    scored_innings_from_runs,
)

SNIPPET = Path(__file__).resolve().parent.parent / "data" / "npb_game_snippet.html"


def test_battery_single_pitcher_no_comma():
    assert _starter_from_battery("村上　‐　坂本") == "村上"


def test_battery_multi_pitcher():
    assert _starter_from_battery("小笠原、堀田　‐　山瀬") == "小笠原"


def test_battery_rejects_homer():
    assert _starter_from_battery("佐藤 22号（6回ソロ 小笠原）") is None


def test_walkoff_x_and_rhe_trim():
    client = NpbClient.__new__(NpbClient)
    runs = client._parse_inning_cells(
        ["0", "0", "0", "0", "1", "0", "0", "0", "4", "1x", "6", "11", "0"]
    )
    assert runs == [0, 0, 0, 0, 1, 0, 0, 0, 4, 1]
    assert scored_innings_from_runs(runs) == [5, 9, 10]


def test_parse_starters_from_snippet():
    html = SNIPPET.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    client = NpbClient.__new__(NpbClient)
    starters = client._parse_starters(soup)
    # Giants id 1 / Tigers id 5 — use whatever match_team returns via names in snippet
    assert any(name == "小笠原" for name in starters.values())
    assert any(name == "村上" for name in starters.values())
    assert not any("号" in (name or "") for name in starters.values())
