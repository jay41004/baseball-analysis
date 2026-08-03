"""CPBL team metadata and name matching."""

from __future__ import annotations

from typing import Any

TEAMS: list[dict[str, Any]] = [
    {
        "id": 1,
        "code": "ACN011",
        "nameZh": "中信兄弟",
        "abbreviation": "兄弟",
        "aliases": ["中信", "兄弟", "中信兄弟"],
    },
    {
        "id": 2,
        "code": "ADD011",
        "nameZh": "統一獅",
        "abbreviation": "統一",
        "aliases": ["統一", "統一獅", "統一7-ELEVEn獅", "7-ELEVEn獅", "獅"],
    },
    {
        "id": 3,
        "code": "AJL011",
        "nameZh": "樂天桃猿",
        "abbreviation": "樂天",
        "aliases": ["樂天", "桃猿", "樂天桃猿"],
    },
    {
        "id": 4,
        "code": "AEO011",
        "nameZh": "富邦悍將",
        "abbreviation": "富邦",
        "aliases": ["富邦", "悍將", "富邦悍將"],
    },
    {
        "id": 5,
        "code": "AAA011",
        "nameZh": "味全龍",
        "abbreviation": "味全",
        "aliases": ["味全", "龍", "味全龍"],
    },
    {
        "id": 6,
        "code": "AKP011",
        "nameZh": "台鋼雄鷹",
        "abbreviation": "台鋼",
        "aliases": ["台鋼", "雄鷹", "台鋼雄鷹"],
    },
]

TEAM_BY_ID = {team["id"]: team for team in TEAMS}
TEAM_BY_CODE = {team["code"]: team for team in TEAMS}

_ALIAS_ENTRIES: list[tuple[str, dict[str, Any]]] = []
for _team in TEAMS:
    for _alias in _team["aliases"]:
        _ALIAS_ENTRIES.append((_alias, _team))
_ALIAS_ENTRIES.sort(key=lambda item: len(item[0]), reverse=True)


def list_teams() -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "id": team["id"],
                "nameZh": team["nameZh"],
                "abbreviation": team["abbreviation"],
            }
            for team in TEAMS
        ],
        key=lambda team: team["id"],
    )


def team_zh(team_id: int | None = None, *, code: str | None = None) -> str:
    if team_id is not None:
        return TEAM_BY_ID[team_id]["nameZh"]
    if code is not None:
        return TEAM_BY_CODE[code]["nameZh"]
    return "未知球隊"


def match_team(text: str) -> dict[str, Any] | None:
    normalized = text.replace(" ", "").replace("　", "")
    for alias, team in _ALIAS_ENTRIES:
        if alias.replace(" ", "").replace("　", "") in normalized:
            return team
    return None


def team_by_code(code: str | None) -> dict[str, Any] | None:
    if not code:
        return None
    return TEAM_BY_CODE.get(code)
