"""MLB Stats API helpers for first-5-inning scoring analysis."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import httpx

from app.team_names import team_name_zh

MLB_BASE = "https://statsapi.mlb.com/api/v1"
UPCOMING_GAME_STATES = {"Preview", "Live", "Scheduled", "Warmup"}


def mlb_schedule_start() -> date:
    """Include yesterday's slate — needed when local date is ahead of MLB officialDate (e.g. Taiwan)."""
    return date.today() - timedelta(days=1)


async def fetch_teams() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{MLB_BASE}/teams", params={"sportId": 1})
        resp.raise_for_status()
        teams = resp.json().get("teams", [])

    return sorted(
        [
            {
                "id": t["id"],
                "name": t["name"],
                "nameZh": team_name_zh(team_id=t["id"], english_name=t["name"]),
                "abbreviation": t["abbreviation"],
                "teamName": t["teamName"],
            }
            for t in teams
            if t.get("active") is not False
        ],
        key=lambda t: t["nameZh"],
    )


async def fetch_recent_final_games(team_id: int, count: int = 10) -> list[dict[str, Any]]:
    end = date.today()
    start = end - timedelta(days=120)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{MLB_BASE}/schedule",
            params={
                "sportId": 1,
                "teamId": team_id,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "gameType": "R",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    games = [
        g
        for d in data.get("dates", [])
        for g in d.get("games", [])
        if g.get("status", {}).get("abstractGameState") == "Final"
        and g.get("status", {}).get("detailedState") != "Postponed"
    ]
    games.sort(key=lambda g: g.get("officialDate", ""), reverse=True)

    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for game in games:
        game_pk = game.get("gamePk")
        if game_pk in seen:
            continue
        seen.add(game_pk)
        unique.append(game)
    return unique[:count]


def _team_side(game: dict[str, Any], team_id: int) -> str:
    if game["teams"]["away"]["team"]["id"] == team_id:
        return "away"
    if game["teams"]["home"]["team"]["id"] == team_id:
        return "home"
    raise ValueError(f"Team {team_id} not in game {game.get('gamePk')}")


def first_five_runs(linescore: dict[str, Any], side: str) -> int:
    total = 0
    for inning in linescore.get("innings", []):
        if inning.get("num", 99) <= 5:
            total += inning.get(side, {}).get("runs", 0) or 0
    return total


def first_five_runs_allowed(linescore: dict[str, Any], team_is_home: bool) -> int:
    opponent_side = "away" if team_is_home else "home"
    return first_five_runs(linescore, opponent_side)


async def fetch_linescore(client: httpx.AsyncClient, game_pk: int) -> dict[str, Any]:
    resp = await client.get(f"{MLB_BASE}/game/{game_pk}/linescore")
    resp.raise_for_status()
    return resp.json()


async def fetch_game_starters(client: httpx.AsyncClient, game_pk: int) -> dict[str, str | None]:
    resp = await client.get(f"{MLB_BASE}/game/{game_pk}/boxscore")
    resp.raise_for_status()
    box = resp.json()

    starters: dict[str, str | None] = {"away": None, "home": None}
    for side in ("away", "home"):
        for pid in box["teams"][side].get("pitchers", []):
            player = box["teams"][side]["players"].get(f"ID{pid}", {})
            pitching = player.get("stats", {}).get("pitching", {})
            if pitching.get("gamesStarted") == 1:
                starters[side] = player.get("person", {}).get("fullName")
                break
    return starters


def opponent_starter(starters: dict[str, str | None], team_side: str) -> str | None:
    opponent_side = "home" if team_side == "away" else "away"
    return starters.get(opponent_side)


def first_inning_runs(linescore: dict[str, Any], side: str) -> int:
    for inning in linescore.get("innings", []):
        if inning.get("num") == 1:
            return inning.get(side, {}).get("runs", 0) or 0
    return 0


def first_inning_runs_allowed(linescore: dict[str, Any], team_is_home: bool) -> int:
    """Fallback: opponent runs in inning 1 from linescore."""
    opponent_side = "away" if team_is_home else "home"
    return first_inning_runs(linescore, opponent_side)


async def fetch_game_feed(client: httpx.AsyncClient, game_pk: int) -> dict[str, Any]:
    resp = await client.get(f"{MLB_BASE}.1/game/{game_pk}/feed/live")
    resp.raise_for_status()
    return resp.json()


def _opp_score_at_half_start(
    feed: dict[str, Any], inning: int, half: str, score_key: str
) -> int:
    score = 0
    for play in feed.get("liveData", {}).get("plays", {}).get("allPlays", []):
        about = play.get("about", {})
        play_inning = about.get("inning")
        play_half = about.get("halfInning")
        if play_inning > inning:
            break
        if play_inning == inning and play_half == half:
            break
        score = play.get("result", {}).get(score_key, 0) or 0
    return score


def pitcher_runs_by_inning_from_linescore(
    linescore: dict[str, Any], is_home: bool, max_inning: int = 9
) -> list[int]:
    opponent_side = "away" if is_home else "home"
    runs = [0] * max_inning
    for inning in linescore.get("innings", []):
        num = inning.get("num", 0)
        if 1 <= num <= max_inning:
            runs[num - 1] = inning.get(opponent_side, {}).get("runs", 0) or 0
    return runs


def _innings_pitched_to_max_inning(innings_pitched: str | float | None) -> int:
    if innings_pitched is None:
        return 9
    try:
        ip = float(innings_pitched)
    except (TypeError, ValueError):
        return 9
    whole = int(ip)
    partial_outs = round((ip - whole) * 10)
    max_inning = whole + (1 if partial_outs > 0 else 0)
    return min(9, max(1, max_inning))


def pitcher_innings_appeared_from_feed(
    feed: dict[str, Any], pitcher_id: int, is_home: bool, max_inning: int = 9
) -> set[int]:
    """Innings where this pitcher threw at least one pitch in their defensive half."""
    defensive_half = "top" if is_home else "bottom"
    pitcher_id = int(pitcher_id)
    appeared: set[int] = set()
    for play in feed.get("liveData", {}).get("plays", {}).get("allPlays", []):
        about = play.get("about", {})
        inning = about.get("inning")
        if not inning or inning > max_inning:
            continue
        if about.get("halfInning") != defensive_half:
            continue
        play_pitcher_id = play.get("matchup", {}).get("pitcher", {}).get("id")
        if play_pitcher_id is not None and int(play_pitcher_id) == pitcher_id:
            appeared.add(inning)
    return appeared


def merge_pitcher_runs_by_inning(
    pbp_runs: list[int],
    linescore_runs: list[int],
    *,
    feed: dict[str, Any] | None = None,
    pitcher_id: int | None = None,
    is_home: bool = False,
    innings_pitched: str | float | None = None,
    earned_runs: int | None = None,
) -> list[int]:
    """Assign full half-inning opponent runs for every inning the pitcher appeared in."""
    del earned_runs  # kept for call-site compatibility

    appeared: set[int] = set()
    if feed is not None and pitcher_id is not None:
        appeared = pitcher_innings_appeared_from_feed(feed, pitcher_id, is_home)
    if not appeared and innings_pitched is not None:
        max_inning = _innings_pitched_to_max_inning(innings_pitched)
        appeared = set(range(1, max_inning + 1))

    length = max(len(pbp_runs), len(linescore_runs), 9)
    merged = [0] * length
    for inning in appeared:
        index = inning - 1
        if 0 <= index < len(linescore_runs):
            merged[index] = linescore_runs[index]
        elif 0 <= index < len(pbp_runs):
            merged[index] = pbp_runs[index]
    return merged[:9]


def pitcher_runs_by_inning_from_feed(
    feed: dict[str, Any], pitcher_id: int, is_home: bool, max_inning: int = 9
) -> list[int]:
    """Runs allowed by this pitcher in each defensive half (index 0 = inning 1)."""
    defensive_half = "top" if is_home else "bottom"
    opponent = "away" if is_home else "home"
    score_key = f"{opponent}Score"
    pitcher_id = int(pitcher_id)
    runs = [0] * max_inning
    half_started: set[int] = set()
    last_baseline: dict[int, int] = {}

    for play in feed.get("liveData", {}).get("plays", {}).get("allPlays", []):
        about = play.get("about", {})
        inning = about.get("inning")
        if not inning or inning > max_inning:
            continue
        if about.get("halfInning") != defensive_half:
            continue
        play_pitcher_id = play.get("matchup", {}).get("pitcher", {}).get("id")
        if play_pitcher_id is None or int(play_pitcher_id) != pitcher_id:
            continue

        result = play.get("result", {})
        opp_score = result.get(score_key, 0) or 0

        if inning not in half_started:
            half_started.add(inning)
            baseline = _opp_score_at_half_start(feed, inning, defensive_half, score_key)
        else:
            baseline = last_baseline.get(inning, opp_score)

        if opp_score > baseline:
            runs[inning - 1] += opp_score - baseline
        last_baseline[inning] = opp_score

    return runs


def pitcher_first_inning_from_feed(
    feed: dict[str, Any], pitcher_id: int, is_home: bool
) -> int:
    runs = pitcher_runs_by_inning_from_feed(feed, pitcher_id, is_home, max_inning=1)
    return runs[0] if runs else 0


def scored_innings_from_runs(runs_by_inning: list[int]) -> list[int]:
    return [index + 1 for index, runs in enumerate(runs_by_inning) if runs > 0]


def summarize_thresholds(runs_list: list[int]) -> dict[str, Any]:
    total = len(runs_list)
    return {
        "totalGames": total,
        "over15": sum(1 for r in runs_list if r > 1.5),
        "under15": sum(1 for r in runs_list if r <= 1.5),
        "over25": sum(1 for r in runs_list if r > 2.5),
        "under25": sum(1 for r in runs_list if r <= 2.5),
        "avgRuns": round(sum(runs_list) / total, 2) if total else 0,
    }


def summarize_pitcher_summary(rows: list[dict[str, Any]], runs_list: list[int]) -> dict[str, Any]:
    total = len(rows)
    inning_scored_counts = {str(inning): 0 for inning in range(1, 10)}
    for row in rows:
        scored = set(row.get("scoredInnings") or [])
        if row.get("firstInningScored"):
            scored.add(1)
        for inning in scored:
            if 1 <= inning <= 9:
                inning_scored_counts[str(inning)] += 1
    first_inning_scored = inning_scored_counts["1"]
    return {
        **summarize_thresholds(runs_list),
        "firstInningScored": first_inning_scored,
        "firstInningClean": total - first_inning_scored,
        "inningScoredCounts": inning_scored_counts,
    }


def summarize_team_scoring(rows: list[dict[str, Any]], runs_list: list[int]) -> dict[str, Any]:
    total = len(rows)
    first_inning_scored = sum(1 for row in rows if row.get("firstInningScored"))
    return {
        **summarize_thresholds(runs_list),
        "firstInningScored": first_inning_scored,
        "firstInningNoScore": total - first_inning_scored,
    }


async def fetch_next_matchup(
    client: httpx.AsyncClient, focus_team_id: int
) -> dict[str, Any] | None:
    start = mlb_schedule_start()
    end = start + timedelta(days=15)
    resp = await client.get(
        f"{MLB_BASE}/schedule",
        params={
            "sportId": 1,
            "teamId": focus_team_id,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "gameType": "R",
            "hydrate": "probablePitcher",
        },
    )
    resp.raise_for_status()
    data = resp.json()

    upcoming: list[dict[str, Any]] = []
    for day in data.get("dates", []):
        for game in day.get("games", []):
            state = game.get("status", {}).get("abstractGameState")
            if state in UPCOMING_GAME_STATES:
                upcoming.append(game)

    if not upcoming:
        return None

    upcoming.sort(key=lambda g: g.get("gameDate", ""))
    game = upcoming[0]

    def side_info(side: str) -> dict[str, Any]:
        team = game["teams"][side]["team"]
        probable = game["teams"][side].get("probablePitcher")
        return {
            "teamId": team["id"],
            "teamName": team_name_zh(team_id=team["id"], english_name=team.get("name")),
            "probablePitcher": (
                {"id": probable["id"], "fullName": probable["fullName"]} if probable else None
            ),
        }

    return {
        "date": game.get("officialDate"),
        "gameDate": game.get("gameDate"),
        "gamePk": game.get("gamePk"),
        "status": game.get("status", {}).get("detailedState"),
        "focusTeamId": focus_team_id,
        "away": side_info("away"),
        "home": side_info("home"),
    }


async def fetch_upcoming_game(client: httpx.AsyncClient, team_id: int) -> dict[str, Any] | None:
    matchup = await fetch_next_matchup(client, team_id)
    if not matchup:
        return None

    side = "home" if matchup["home"]["teamId"] == team_id else "away"
    opponent = matchup["home"] if side == "away" else matchup["away"]
    return {
        "date": matchup["date"],
        "gamePk": matchup["gamePk"],
        "gameTime": None,
        "status": matchup["status"],
        "teamName": matchup[side]["teamName"],
        "opponent": opponent["teamName"],
        "isHome": side == "home",
        "probablePitcher": matchup[side]["probablePitcher"],
    }


async def fetch_pitcher_starts(
    client: httpx.AsyncClient, pitcher_id: int, count: int, season: int | None = None
) -> list[dict[str, Any]]:
    season = season or date.today().year
    resp = await client.get(
        f"{MLB_BASE}/people/{pitcher_id}/stats",
        params={"stats": "gameLog", "group": "pitching", "season": season},
    )
    resp.raise_for_status()
    stats = resp.json().get("stats", [])
    if not stats:
        return []

    starts = [
        split
        for split in stats[0].get("splits", [])
        if split.get("stat", {}).get("gamesStarted") == 1
    ]
    starts.sort(key=lambda s: s.get("date", ""), reverse=True)
    return starts[:count]


async def analyze_pitcher_first_five_starts(
    client: httpx.AsyncClient, pitcher_id: int, pitcher_name: str, count: int = 10
) -> dict[str, Any]:
    starts = await fetch_pitcher_starts(client, pitcher_id, count)
    if not starts:
        empty_summary = summarize_pitcher_summary([], [])
        return {
            "pitcherId": pitcher_id,
            "pitcherName": pitcher_name,
            "games": [],
            "summary": empty_summary,
        }

    game_pks = [split["game"]["gamePk"] for split in starts]
    linescores, starters_list, feeds = await asyncio.gather(
        asyncio.gather(*[fetch_linescore(client, pk) for pk in game_pks]),
        asyncio.gather(*[fetch_game_starters(client, pk) for pk in game_pks]),
        asyncio.gather(*[fetch_game_feed(client, pk) for pk in game_pks]),
    )

    rows: list[dict[str, Any]] = []
    for split, linescore, starters, feed in zip(starts, linescores, starters_list, feeds):
        is_home = split.get("isHome", False)
        runs_allowed = first_five_runs_allowed(linescore, is_home)
        stat = split.get("stat", {})
        innings_pitched = stat.get("inningsPitched")
        earned_runs = stat.get("earnedRuns")
        pbp_runs = pitcher_runs_by_inning_from_feed(feed, pitcher_id, is_home)
        linescore_runs = pitcher_runs_by_inning_from_linescore(linescore, is_home)
        runs_by_inning = merge_pitcher_runs_by_inning(
            pbp_runs,
            linescore_runs,
            feed=feed,
            pitcher_id=pitcher_id,
            is_home=is_home,
            innings_pitched=innings_pitched,
            earned_runs=earned_runs,
        )
        first_inning_runs = runs_by_inning[0] if runs_by_inning else 0
        scored_innings = scored_innings_from_runs(runs_by_inning)
        opponent_info = split.get("opponent", {})
        team_side = "home" if split.get("isHome") else "away"
        rows.append(
            {
                "date": split.get("date"),
                "gamePk": split["game"]["gamePk"],
                "opponent": team_name_zh(
                    team_id=opponent_info.get("id"),
                    english_name=opponent_info.get("name", "Unknown"),
                ),
                "opponentStarter": opponent_starter(starters, team_side),
                "isHome": split.get("isHome", False),
                "firstFiveRunsAllowed": runs_allowed,
                "firstInningRunsAllowed": first_inning_runs,
                "firstInningScored": first_inning_runs > 0,
                "runsByInning": runs_by_inning,
                "scoredInnings": scored_innings,
                "over15": runs_allowed > 1.5,
                "over25": runs_allowed > 2.5,
                "inningsPitched": innings_pitched,
                "earnedRuns": earned_runs,
                "result": split.get("isWin"),
            }
        )

    runs_list = [r["firstFiveRunsAllowed"] for r in rows]
    return {
        "pitcherId": pitcher_id,
        "pitcherName": pitcher_name,
        "games": rows,
        "summary": summarize_pitcher_summary(rows, runs_list),
    }


async def analyze_team_scoring(
    client: httpx.AsyncClient, team_id: int, game_count: int = 10
) -> dict[str, Any]:
    games = await fetch_recent_final_games(team_id, game_count)
    if not games:
        return {
            "teamId": team_id,
            "teamName": team_name_zh(team_id=team_id),
            "games": [],
            "summary": summarize_team_scoring([], []),
        }

    game_pks = [game["gamePk"] for game in games]
    linescores, starters_list = await asyncio.gather(
        asyncio.gather(*[fetch_linescore(client, pk) for pk in game_pks]),
        asyncio.gather(*[fetch_game_starters(client, pk) for pk in game_pks]),
    )

    rows: list[dict[str, Any]] = []
    for game, linescore, starters in zip(games, linescores, starters_list):
        side = _team_side(game, team_id)
        opponent_info = game["teams"]["home" if side == "away" else "away"]["team"]
        is_home = side == "home"
        runs = first_five_runs(linescore, side)
        inning_one_runs = first_inning_runs(linescore, side)

        rows.append(
            {
                "date": game.get("officialDate"),
                "gamePk": game.get("gamePk"),
                "opponent": team_name_zh(
                    team_id=opponent_info.get("id"),
                    english_name=opponent_info.get("name"),
                ),
                "opponentStarter": opponent_starter(starters, side),
                "teamStarter": starters.get(side),
                "isHome": is_home,
                "firstInningRuns": inning_one_runs,
                "firstInningScored": inning_one_runs > 0,
                "firstFiveRuns": runs,
                "over15": runs > 1.5,
                "over25": runs > 2.5,
                "result": game["teams"][side].get("isWinner"),
            }
        )

    runs_list = [r["firstFiveRuns"] for r in rows]
    return {
        "teamId": team_id,
        "teamName": team_name_zh(team_id=team_id),
        "games": rows,
        "summary": summarize_team_scoring(rows, runs_list),
    }


async def _build_side_panel(
    client: httpx.AsyncClient, side_info: dict[str, Any], game_count: int
) -> dict[str, Any]:
    scoring = await analyze_team_scoring(client, side_info["teamId"], game_count)
    pitcher_analysis = None
    probable = side_info.get("probablePitcher")
    if probable:
        pitcher_analysis = await analyze_pitcher_first_five_starts(
            client, probable["id"], probable["fullName"], game_count
        )

    return {
        **scoring,
        "probablePitcher": probable,
        "pitcherAnalysis": pitcher_analysis,
    }


async def analyze_matchup(focus_team_id: int, game_count: int = 10) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        matchup = await fetch_next_matchup(client, focus_team_id)
        if not matchup:
            raise ValueError("找不到下一場比賽")

        away_panel, home_panel = await asyncio.gather(
            _build_side_panel(client, matchup["away"], game_count),
            _build_side_panel(client, matchup["home"], game_count),
        )

    return {
        "focusTeamId": focus_team_id,
        "matchup": {
            "date": matchup["date"],
            "gameDate": matchup.get("gameDate"),
            "gamePk": matchup["gamePk"],
            "status": matchup["status"],
        },
        "away": away_panel,
        "home": home_panel,
    }


async def analyze_team_first_five(team_id: int, game_count: int = 10) -> dict[str, Any]:
    games = await fetch_recent_final_games(team_id, game_count)
    rows: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for game in games:
            side = _team_side(game, team_id)
            opponent_info = game["teams"]["home" if side == "away" else "away"]["team"]
            is_home = side == "home"

            linescore = await fetch_linescore(client, game["gamePk"])
            runs = first_five_runs(linescore, side)

            rows.append(
                {
                    "date": game.get("officialDate"),
                    "gamePk": game.get("gamePk"),
                    "opponent": team_name_zh(
                        team_id=opponent_info.get("id"),
                        english_name=opponent_info.get("name"),
                    ),
                    "isHome": is_home,
                    "firstFiveRuns": runs,
                    "over15": runs > 1.5,
                    "over25": runs > 2.5,
                    "result": game["teams"][side].get("isWinner"),
                }
            )

        next_game = await fetch_upcoming_game(client, team_id)

        pitcher_analysis = None
        if next_game and next_game.get("probablePitcher"):
            pitcher = next_game["probablePitcher"]
            pitcher_analysis = await analyze_pitcher_first_five_starts(
                client, pitcher["id"], pitcher["fullName"], game_count
            )

    runs_list = [r["firstFiveRuns"] for r in rows]
    return {
        "teamId": team_id,
        "teamName": team_name_zh(team_id=team_id),
        "games": rows,
        "summary": summarize_thresholds(runs_list),
        "nextGame": next_game,
        "pitcherAnalysis": pitcher_analysis,
    }
