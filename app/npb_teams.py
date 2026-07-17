"""NPB team metadata and name matching."""

from __future__ import annotations

from typing import Any

TEAMS: list[dict[str, Any]] = [
    {
        "id": 1,
        "code": "g",
        "nameJa": "巨人",
        "nameZh": "讀賣巨人",
        "abbreviation": "G",
        "league": "CL",
        "aliases": ["読売ジャイアンツ", "読売", "巨人", "ジャイアンツ"],
    },
    {
        "id": 2,
        "code": "t",
        "nameJa": "阪神",
        "nameZh": "阪神虎",
        "abbreviation": "T",
        "league": "CL",
        "aliases": ["阪神タイガース", "阪神", "タイガース"],
    },
    {
        "id": 3,
        "code": "d",
        "nameJa": "中日",
        "nameZh": "中日龍",
        "abbreviation": "D",
        "league": "CL",
        "aliases": ["中日ドラゴンズ", "中日", "ドラゴンズ"],
    },
    {
        "id": 4,
        "code": "c",
        "nameJa": "広島",
        "nameZh": "廣島東洋鯉",
        "abbreviation": "C",
        "league": "CL",
        "aliases": ["広島東洋カープ", "広島", "カープ"],
    },
    {
        "id": 5,
        "code": "s",
        "nameJa": "ヤクルト",
        "nameZh": "東京養樂多",
        "abbreviation": "S",
        "league": "CL",
        "aliases": ["東京ヤクルトスワローズ", "ヤクルト", "スワローズ"],
    },
    {
        "id": 6,
        "code": "db",
        "nameJa": "DeNA",
        "nameZh": "橫濱 DeNA",
        "abbreviation": "DB",
        "league": "CL",
        "aliases": ["横浜DeNAベイスターズ", "横浜DeNA", "DeNA", "ベイスターズ"],
    },
    {
        "id": 7,
        "code": "h",
        "nameJa": "ソフトバンク",
        "nameZh": "福岡软银",
        "abbreviation": "H",
        "league": "PL",
        "aliases": ["福岡ソフトバンクホークス", "ソフトバンク", "ホークス"],
    },
    {
        "id": 8,
        "code": "f",
        "nameJa": "日本ハム",
        "nameZh": "日本火腿",
        "abbreviation": "F",
        "league": "PL",
        "aliases": ["北海道日本ハムファイターズ", "日本ハム", "ファイターズ"],
    },
    {
        "id": 9,
        "code": "b",
        "nameJa": "オリックス",
        "nameZh": "欧力士",
        "abbreviation": "B",
        "league": "PL",
        "aliases": ["オリックス・バファローズ", "オリックス", "バファローズ"],
    },
    {
        "id": 10,
        "code": "e",
        "nameJa": "楽天",
        "nameZh": "东北乐天",
        "abbreviation": "E",
        "league": "PL",
        "aliases": ["東北楽天ゴールデンイーグルス", "楽天", "イーグルス"],
    },
    {
        "id": 11,
        "code": "l",
        "nameJa": "西武",
        "nameZh": "埼玉西武",
        "abbreviation": "L",
        "league": "PL",
        "aliases": ["埼玉西武ライオンズ", "西武", "ライオンズ"],
    },
    {
        "id": 12,
        "code": "m",
        "nameJa": "ロッテ",
        "nameZh": "千叶罗德",
        "abbreviation": "M",
        "league": "PL",
        "aliases": ["千葉ロッテマリーンズ", "ロッテ", "マリーンズ"],
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
                "nameJa": team["nameJa"],
                "nameZh": team["nameZh"],
                "abbreviation": team["abbreviation"],
                "league": team["league"],
            }
            for team in TEAMS
        ],
        key=lambda team: (team["league"], team["id"]),
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


def team_in_text(text: str, team: dict[str, Any]) -> bool:
    normalized = text.replace(" ", "").replace("　", "")
    return any(alias.replace(" ", "").replace("　", "") in normalized for alias in team["aliases"])
