"""Convert NPB Japanese strings to Chinese or English for the UI."""

from __future__ import annotations

import re

_KATAKANA_RE = re.compile(r"[\u30a0-\u30ff\uff65-\uff9f]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Manual overrides for nicknames / common Taiwan baseball spellings.
_PITCHER_OVERRIDES: dict[str, str] = {
    "ジェリー": "Jerry",
    "細野": "细野",
    "細野晴希": "细野",
}

_STADIUM_OVERRIDES: dict[str, str] = {
    "マツダスタジアム": "马自达球场",
    "東京ドーム": "东京巨蛋",
    "京セラドーム大阪": "京瓷大阪巨蛋",
    "京セラD大阪": "京瓷大阪巨蛋",
    "バンテリンドーム": "名古屋巨蛋",
    "エスコンフィールド": "ES CON Field",
    "みずほPayPayドーム": "PayPay Dome",
    "ペイペイドーム": "PayPay Dome",
    "ZOZOマリン": "ZOZO Marine",
    "ベルーナドーム": "Belluna Dome",
    "楽天モバイルパーク": "乐天移动公园",
    "横浜": "横滨",
    "横　浜": "横滨",
    "甲子園": "甲子园",
    "明治神宮": "明治神宫",
    "神宮": "神宫球场",
}

# Katakana -> romaji (Hepburn). Longer sequences first.
_KATAKANA_ROMAN: list[tuple[str, str]] = [
    ("キャ", "kya"),
    ("キュ", "kyu"),
    ("キョ", "kyo"),
    ("ギャ", "gya"),
    ("ギュ", "gyu"),
    ("ギョ", "gyo"),
    ("シャ", "sha"),
    ("シュ", "shu"),
    ("ショ", "sho"),
    ("ジャ", "ja"),
    ("ジュ", "ju"),
    ("ジョ", "jo"),
    ("チャ", "cha"),
    ("チュ", "chu"),
    ("チョ", "cho"),
    ("ニャ", "nya"),
    ("ニュ", "nyu"),
    ("ニョ", "nyo"),
    ("ヒャ", "hya"),
    ("ヒュ", "hyu"),
    ("ヒョ", "hyo"),
    ("ビャ", "bya"),
    ("ビュ", "byu"),
    ("ビョ", "byo"),
    ("ピャ", "pya"),
    ("ピュ", "pyu"),
    ("ピョ", "pyo"),
    ("ミャ", "mya"),
    ("ミュ", "myu"),
    ("ミョ", "myo"),
    ("リャ", "rya"),
    ("リュ", "ryu"),
    ("リョ", "ryo"),
    ("ファ", "fa"),
    ("フィ", "fi"),
    ("フェ", "fe"),
    ("フォ", "fo"),
    ("ウィ", "wi"),
    ("ウェ", "we"),
    ("ウォ", "wo"),
    ("ヴァ", "va"),
    ("ヴィ", "vi"),
    ("ヴ", "vu"),
    ("ヴェ", "ve"),
    ("ヴォ", "vo"),
    ("ー", "-"),
    ("ア", "a"),
    ("イ", "i"),
    ("ウ", "u"),
    ("エ", "e"),
    ("オ", "o"),
    ("カ", "ka"),
    ("キ", "ki"),
    ("ク", "ku"),
    ("ケ", "ke"),
    ("コ", "ko"),
    ("ガ", "ga"),
    ("ギ", "gi"),
    ("グ", "gu"),
    ("ゲ", "ge"),
    ("ゴ", "go"),
    ("サ", "sa"),
    ("シ", "shi"),
    ("ス", "su"),
    ("セ", "se"),
    ("ソ", "so"),
    ("ザ", "za"),
    ("ジ", "ji"),
    ("ズ", "zu"),
    ("ゼ", "ze"),
    ("ゾ", "zo"),
    ("タ", "ta"),
    ("チ", "chi"),
    ("ツ", "tsu"),
    ("テ", "te"),
    ("ト", "to"),
    ("ダ", "da"),
    ("ヂ", "ji"),
    ("ヅ", "zu"),
    ("デ", "de"),
    ("ド", "do"),
    ("ナ", "na"),
    ("ニ", "ni"),
    ("ヌ", "nu"),
    ("ネ", "ne"),
    ("ノ", "no"),
    ("ハ", "ha"),
    ("ヒ", "hi"),
    ("フ", "fu"),
    ("ヘ", "he"),
    ("ホ", "ho"),
    ("バ", "ba"),
    ("ビ", "bi"),
    ("ブ", "bu"),
    ("ベ", "be"),
    ("ボ", "bo"),
    ("パ", "pa"),
    ("ピ", "pi"),
    ("プ", "pu"),
    ("ペ", "pe"),
    ("ポ", "po"),
    ("マ", "ma"),
    ("ミ", "mi"),
    ("ム", "mu"),
    ("メ", "me"),
    ("モ", "mo"),
    ("ヤ", "ya"),
    ("ユ", "yu"),
    ("ヨ", "yo"),
    ("ラ", "ra"),
    ("リ", "ri"),
    ("ル", "ru"),
    ("レ", "re"),
    ("ロ", "ro"),
    ("ワ", "wa"),
    ("ヲ", "wo"),
    ("ン", "n"),
    ("ッ", ""),
    ("ァ", "a"),
    ("ィ", "i"),
    ("ゥ", "u"),
    ("ェ", "e"),
    ("ォ", "o"),
    ("ャ", "ya"),
    ("ュ", "yu"),
    ("ョ", "yo"),
    ("ヮ", "wa"),
    ("ヵ", "ka"),
    ("ヶ", "ke"),
    ("・", " "),
    ("･", " "),
]

_HALF_WIDTH: dict[str, str] = {
    "ｱ": "ア",
    "ｲ": "イ",
    "ｳ": "ウ",
    "ｴ": "エ",
    "ｵ": "オ",
    "ｶ": "カ",
    "ｷ": "キ",
    "ｸ": "ク",
    "ｹ": "ケ",
    "ｺ": "コ",
    "ｻ": "サ",
    "ｼ": "シ",
    "ｽ": "ス",
    "ｾ": "セ",
    "ｿ": "ソ",
    "ﾀ": "タ",
    "ﾁ": "チ",
    "ﾂ": "ツ",
    "ﾃ": "テ",
    "ﾄ": "ト",
    "ﾅ": "ナ",
    "ﾆ": "ニ",
    "ﾇ": "ヌ",
    "ﾈ": "ネ",
    "ﾉ": "ノ",
    "ﾊ": "ハ",
    "ﾋ": "ヒ",
    "ﾌ": "フ",
    "ﾍ": "ヘ",
    "ﾎ": "ホ",
    "ﾏ": "マ",
    "ﾐ": "ミ",
    "ﾑ": "ム",
    "ﾒ": "メ",
    "ﾓ": "モ",
    "ﾔ": "ヤ",
    "ﾕ": "ユ",
    "ﾖ": "ヨ",
    "ﾗ": "ラ",
    "ﾘ": "リ",
    "ﾙ": "ル",
    "ﾚ": "レ",
    "ﾛ": "ロ",
    "ﾜ": "ワ",
    "ｦ": "ヲ",
    "ﾝ": "ン",
    "ｧ": "ァ",
    "ｨ": "ィ",
    "ｩ": "ゥ",
    "ｪ": "ェ",
    "ｫ": "ォ",
    "ｬ": "ャ",
    "ｭ": "ュ",
    "ｮ": "ョ",
    "ｯ": "ッ",
    "ｰ": "ー",
    "･": "・",
}


def _normalize_key(text: str) -> str:
    return text.replace(" ", "").replace("　", "").strip()


def _to_fullwidth_katakana(text: str) -> str:
    chars: list[str] = []
    for char in text:
        if char in _HALF_WIDTH:
            chars.append(_HALF_WIDTH[char])
            continue
        chars.append(char)
    return "".join(chars)


def _has_katakana(text: str) -> bool:
    return bool(_KATAKANA_RE.search(text))


def _katakana_to_romaji(text: str) -> str:
    source = _to_fullwidth_katakana(text)
    pieces: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char == "ッ" and index + 1 < len(source):
            next_romaji = ""
            for kana, romaji in _KATAKANA_ROMAN:
                if kana and source.startswith(kana, index + 1):
                    next_romaji = romaji
                    break
            if next_romaji:
                consonant = next_romaji[0] if next_romaji and next_romaji[0].isalpha() else ""
                if consonant:
                    pieces.append(consonant)
            index += 1
            continue

        matched = False
        for kana, romaji in _KATAKANA_ROMAN:
            if kana and source.startswith(kana, index):
                pieces.append(romaji)
                index += len(kana)
                matched = True
                break
        if matched:
            continue

        if char in {" ", "　"}:
            pieces.append(" ")
        elif char == "-":
            pieces.append("-")
        else:
            pieces.append(char)
        index += 1

    result = "".join(pieces)
    result = re.sub(r"-+", "-", result)
    result = re.sub(r"\s+", " ", result).strip(" -")
    return _title_case_name(result)


def _title_case_name(text: str) -> str:
    parts = []
    for chunk in re.split(r"(\s+|-)", text):
        if not chunk or chunk.isspace() or chunk == "-":
            parts.append(chunk)
            continue
        if chunk.isupper():
            parts.append(chunk)
            continue
        parts.append(chunk[:1].upper() + chunk[1:].lower())
    return "".join(parts)


def _apply_overrides(text: str, overrides: dict[str, str]) -> str | None:
    normalized = _normalize_key(text)
    if normalized in overrides:
        return overrides[normalized]
    for key, value in overrides.items():
        if key in normalized:
            return value
    return None


def display_pitcher_name(raw: str | None) -> str | None:
    if not raw:
        return raw

    override = _apply_overrides(raw, _PITCHER_OVERRIDES)
    if override:
        return override

    if _has_katakana(raw):
        return _katakana_to_romaji(raw)

    # Kanji-only names are readable in zh-Hant; keep as-is.
    if _CJK_RE.search(raw):
        return raw.strip()

    return raw.strip()


def display_stadium(raw: str | None) -> str | None:
    if not raw:
        return raw

    time_match = re.search(r"\s+(\d{1,2}:\d{2})\s*$", raw)
    time_suffix = f" {time_match.group(1)}" if time_match else ""
    name = raw[: time_match.start()] if time_match else raw
    compact = _normalize_key(name)

    for key, value in _STADIUM_OVERRIDES.items():
        if _normalize_key(key) in compact or compact in _normalize_key(key):
            return f"{value}{time_suffix}"

    if _has_katakana(name):
        return f"{_katakana_to_romaji(name)}{time_suffix}"

    return raw.strip()


def localize_side_games(games: list[dict], *, starter_key: str = "opponentStarter") -> None:
    for game in games:
        starter = game.get(starter_key)
        if starter:
            game[starter_key] = display_pitcher_name(starter)


def localize_pitcher_analysis(analysis: dict | None) -> None:
    if not analysis:
        return
    if analysis.get("pitcherName"):
        analysis["pitcherName"] = display_pitcher_name(analysis["pitcherName"])
    localize_side_games(analysis.get("games") or [])


def localize_probable_pitcher(probable: dict | None) -> None:
    if not probable:
        return
    full_name = probable.get("fullName")
    if full_name:
        probable["fullName"] = display_pitcher_name(full_name)


def localize_matchup_payload(payload: dict) -> None:
    matchup = payload.get("matchup")
    if matchup:
        stadium = matchup.get("stadium")
        if stadium:
            matchup["stadium"] = display_stadium(stadium)
        for side in ("away", "home"):
            side_info = matchup.get(side)
            if side_info:
                localize_probable_pitcher(side_info.get("probablePitcher"))

    for side in ("away", "home"):
        panel = payload.get(side)
        if not panel:
            continue
        localize_probable_pitcher(panel.get("probablePitcher"))
        localize_side_games(panel.get("games") or [])
        localize_pitcher_analysis(panel.get("pitcherAnalysis"))
