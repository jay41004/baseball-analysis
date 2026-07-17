"""MLB team Chinese display names."""

from __future__ import annotations

TEAM_ZH_BY_ID: dict[int, str] = {
    108: "天使",
    109: "響尾蛇",
    110: "金鶯",
    111: "紅襪",
    112: "小熊",
    113: "紅人",
    114: "守護者",
    115: "落磯",
    116: "老虎",
    117: "太空人",
    118: "皇家",
    119: "道奇",
    120: "國民",
    121: "大都會",
    133: "運動家",
    134: "海盜",
    135: "教士",
    136: "水手",
    137: "巨人",
    138: "紅雀",
    139: "光芒",
    140: "遊騎兵",
    141: "藍鳥",
    142: "雙城",
    143: "費城人",
    144: "勇士",
    145: "白襪",
    146: "馬林魚",
    147: "洋基",
    158: "釀酒人",
}

TEAM_ZH_BY_ENGLISH: dict[str, str] = {
    "Arizona Diamondbacks": "響尾蛇",
    "Athletics": "運動家",
    "Oakland Athletics": "運動家",
    "Atlanta Braves": "勇士",
    "Baltimore Orioles": "金鶯",
    "Boston Red Sox": "紅襪",
    "Chicago Cubs": "小熊",
    "Chicago White Sox": "白襪",
    "Cincinnati Reds": "紅人",
    "Cleveland Guardians": "守護者",
    "Colorado Rockies": "落磯",
    "Detroit Tigers": "老虎",
    "Houston Astros": "太空人",
    "Kansas City Royals": "皇家",
    "Los Angeles Angels": "天使",
    "Los Angeles Dodgers": "道奇",
    "Miami Marlins": "馬林魚",
    "Milwaukee Brewers": "釀酒人",
    "Minnesota Twins": "雙城",
    "New York Mets": "大都會",
    "New York Yankees": "洋基",
    "Philadelphia Phillies": "費城人",
    "Pittsburgh Pirates": "海盜",
    "San Diego Padres": "教士",
    "San Francisco Giants": "巨人",
    "Seattle Mariners": "水手",
    "St. Louis Cardinals": "紅雀",
    "Tampa Bay Rays": "光芒",
    "Texas Rangers": "遊騎兵",
    "Toronto Blue Jays": "藍鳥",
    "Washington Nationals": "國民",
}


def team_name_zh(*, team_id: int | None = None, english_name: str | None = None) -> str:
    if team_id is not None and team_id in TEAM_ZH_BY_ID:
        return TEAM_ZH_BY_ID[team_id]
    if english_name:
        return TEAM_ZH_BY_ENGLISH.get(english_name, english_name)
    return english_name or "未知球隊"


def localize_analysis(data: dict) -> dict:
    team_id = data.get("teamId")
    if team_id is not None:
        data["teamName"] = team_name_zh(team_id=team_id, english_name=data.get("teamName"))

    for game in data.get("games", []):
        game["opponent"] = team_name_zh(english_name=game.get("opponent"))

    next_game = data.get("nextGame")
    if next_game:
        if team_id is not None:
            next_game["teamName"] = team_name_zh(team_id=team_id, english_name=next_game.get("teamName"))
        next_game["opponent"] = team_name_zh(english_name=next_game.get("opponent"))

    pitcher = data.get("pitcherAnalysis")
    if pitcher:
        for game in pitcher.get("games", []):
            game["opponent"] = team_name_zh(english_name=game.get("opponent"))

    return data
