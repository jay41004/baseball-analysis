"""MLB Stats API helpers for first-5-inning scoring analysis."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import httpx

from app.inning_comparison import (
    build_inning_comparison,
    build_matchup_situational,
    strip_panel_internals,
)
from app.team_names import team_name_zh

MLB_BASE = "https://statsapi.mlb.com/api/v1"
UPCOMING_GAME_STATES = {"Preview", "Live", "Scheduled", "Warmup"}
_MLB_FETCH_SEM = asyncio.Semaphore(16)


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


async def fetch_recent_final_games(
    team_id: int,
    count: int = 10,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    end = date.today()
    start = end - timedelta(days=120)
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "gameType": "R",
    }

    if client is None:
        async with httpx.AsyncClient(timeout=30.0) as owned:
            resp = await owned.get(f"{MLB_BASE}/schedule", params=params)
            resp.raise_for_status()
            data = resp.json()
    else:
        resp = await client.get(f"{MLB_BASE}/schedule", params=params)
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
    async with _MLB_FETCH_SEM:
        resp = await client.get(f"{MLB_BASE}/game/{game_pk}/linescore")
    resp.raise_for_status()
    return resp.json()


async def fetch_game_boxscore(client: httpx.AsyncClient, game_pk: int) -> dict[str, Any]:
    async with _MLB_FETCH_SEM:
        resp = await client.get(f"{MLB_BASE}/game/{game_pk}/boxscore")
    resp.raise_for_status()
    return resp.json()


async def fetch_game_starters(client: httpx.AsyncClient, game_pk: int) -> dict[str, str | None]:
    box = await fetch_game_boxscore(client, game_pk)

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


def _scores_from_linescore(linescore: dict[str, Any], is_home: bool) -> tuple[int | None, int | None]:
    teams = linescore.get("teams") or {}
    away_runs = teams.get("away", {}).get("runs")
    home_runs = teams.get("home", {}).get("runs")
    if away_runs is None or home_runs is None:
        return None, None
    if is_home:
        return home_runs, away_runs
    return away_runs, home_runs


def _batting_slot_and_seq(batting_order: Any) -> tuple[int, int] | None:
    """Parse MLB battingOrder codes like 100/401 into (slot, sequence).

    Starters are the lowest sequence in each slot (usually xx00; older seasons used xx01).
    Substitutes get higher sequences (401, 502, …), so the team battingOrder array often
    ends up listing the final occupant — not the original starter.
    """
    text = str(batting_order or "").strip()
    if len(text) < 3 or not text.isdigit():
        return None
    try:
        slot = int(text[:-2])
        seq = int(text[-2:])
    except ValueError:
        return None
    if not 1 <= slot <= 9:
        return None
    return slot, seq


def _batter_entry_from_player(order: int, player: dict[str, Any]) -> dict[str, Any]:
    person = player.get("person", {})
    season = player.get("seasonStats", {}).get("batting", {})
    at_bats = season.get("atBats")
    hits = season.get("hits")
    entry: dict[str, Any] = {
        "order": order,
        "id": person.get("id"),
        "name": person.get("fullName"),
        "position": player.get("position", {}).get("abbreviation", ""),
        "avg": season.get("avg"),
        "atBats": at_bats,
        "hits": hits,
        "obp": season.get("obp"),
        "slg": season.get("slg"),
        "ops": season.get("ops"),
        "homeRuns": season.get("homeRuns"),
        "rbi": season.get("rbi"),
        "runs": season.get("runs"),
    }
    if at_bats is not None and hits is not None:
        entry["abHits"] = f"{int(at_bats)}-{int(hits)}"
    return entry


def _parse_batters_from_team_box(team_data: dict[str, Any]) -> list[dict[str, Any]]:
    players = team_data.get("players") or {}
    # Prefer original starters: non-substitutes with the lowest sequence per slot.
    # MLB team battingOrder lists the final occupant (often a late sub), so do not trust it alone.
    starters_by_slot: dict[int, tuple[tuple[int, int], dict[str, Any]]] = {}
    for player in players.values():
        parsed = _batting_slot_and_seq(player.get("battingOrder"))
        if parsed is None:
            continue
        slot, seq = parsed
        is_sub = bool((player.get("gameStatus") or {}).get("isSubstitute"))
        # Rank: starters first (0), then lower sequence number.
        rank = (1 if is_sub else 0, seq)
        current = starters_by_slot.get(slot)
        if current is None or rank < current[0]:
            starters_by_slot[slot] = (rank, player)

    if starters_by_slot:
        return [
            _batter_entry_from_player(slot, starters_by_slot[slot][1])
            for slot in sorted(starters_by_slot)
        ]

    # Pregame / incomplete boxes may only expose the battingOrder id list.
    batting_order = team_data.get("battingOrder") or []
    batters: list[dict[str, Any]] = []
    for order, player_id in enumerate(batting_order, start=1):
        player = players.get(f"ID{player_id}", {})
        if not player:
            continue
        if bool((player.get("gameStatus") or {}).get("isSubstitute")):
            continue
        batters.append(_batter_entry_from_player(order, player))
    return batters


# Bump when lineup parsing / enrichment rules change so cached cards rebuild.
LINEUP_LOGIC_VERSION = 3


def lineups_need_rebuild(lineups: dict[str, Any] | None) -> bool:
    if not lineups:
        return True
    if lineups.get("logicVersion") != LINEUP_LOGIC_VERSION:
        return True
    away = len((lineups.get("away") or {}).get("batters") or [])
    home = len((lineups.get("home") or {}).get("batters") or [])
    return away == 0 and home == 0



def _format_batting_avg(hits: int, at_bats: int) -> str | None:
    if at_bats <= 0:
        return None
    return f"{hits / at_bats:.3f}"[1:]


def _recent_batting_form(splits: list[dict[str, Any]]) -> dict[str, Any]:
    recent3 = splits[:3]
    recent5 = splits[:5]

    def totals(games: list[dict[str, Any]]) -> tuple[int, int, int]:
        hits = 0
        at_bats = 0
        hit_games = 0
        for split in games:
            stat = split.get("stat", {})
            game_hits = stat.get("hits") or 0
            game_ab = stat.get("atBats") or 0
            hits += game_hits
            at_bats += game_ab
            if game_hits > 0:
                hit_games += 1
        return hit_games, hits, at_bats

    hit_games_3, hits_3, ab_3 = totals(recent3)
    _, hits_5, ab_5 = totals(recent5)

    return {
        "recent3HitGames": hit_games_3,
        "recent3Games": len(recent3),
        "recent3Avg": _format_batting_avg(hits_3, ab_3),
        "recent5Avg": _format_batting_avg(hits_5, ab_5),
    }


async def fetch_batter_hitting_game_log(
    client: httpx.AsyncClient, player_id: int, *, season: int | None = None
) -> list[dict[str, Any]]:
    season = season or date.today().year
    async with _MLB_FETCH_SEM:
        resp = await client.get(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "gameLog", "group": "hitting", "season": season},
        )
    resp.raise_for_status()
    stats = resp.json().get("stats", [])
    if not stats:
        return []
    splits = stats[0].get("splits", [])
    splits.sort(key=lambda split: split.get("date", ""), reverse=True)
    return splits


async def enrich_batters_recent_form(
    client: httpx.AsyncClient, batters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    player_ids = [batter["id"] for batter in batters if batter.get("id")]
    if not player_ids:
        return batters

    logs = await asyncio.gather(
        *[fetch_batter_hitting_game_log(client, player_id) for player_id in player_ids]
    )
    form_by_id = {
        player_id: _recent_batting_form(log)
        for player_id, log in zip(player_ids, logs)
    }
    for batter in batters:
        player_id = batter.get("id")
        if player_id in form_by_id:
            batter.update(form_by_id[player_id])
    return batters


async def _fetch_previous_starting_lineup(
    client: httpx.AsyncClient, team_id: int
) -> tuple[list[dict[str, Any]], str | None]:
    games = await fetch_recent_final_games(team_id, 1, client=client)
    if not games:
        return [], None
    game = games[0]
    side = _team_side(game, team_id)
    box = await fetch_game_boxscore(client, game["gamePk"])
    batters = _parse_batters_from_team_box(box["teams"][side])
    return batters, game.get("officialDate")


async def fetch_batter_risp_avg(
    client: httpx.AsyncClient, batter_id: int, *, season: int | None = None
) -> str | None:
    season = season or date.today().year
    async with _MLB_FETCH_SEM:
        resp = await client.get(
            f"{MLB_BASE}/people/{batter_id}/stats",
            params={
                "stats": "statSplits",
                "group": "hitting",
                "season": season,
                "sitCodes": "risp",
            },
        )
    if resp.status_code != 200:
        return None
    stats = resp.json().get("stats") or []
    if not stats:
        return None
    splits = stats[0].get("splits") or []
    if not splits:
        return None
    avg = splits[0].get("stat", {}).get("avg")
    if avg is None:
        return None
    try:
        text = f"{float(avg):.3f}"
    except (TypeError, ValueError):
        return None
    return text[1:] if text.startswith("0.") else text


async def enrich_batters_risp(
    client: httpx.AsyncClient, batters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    player_ids = [batter["id"] for batter in batters if batter.get("id")]
    if not player_ids:
        return batters
    risp_values = await asyncio.gather(
        *[fetch_batter_risp_avg(client, int(player_id)) for player_id in player_ids]
    )
    enriched: list[dict[str, Any]] = []
    for batter, risp_avg in zip(batters, risp_values):
        copy = dict(batter)
        if risp_avg:
            copy["rispAvg"] = risp_avg
        enriched.append(copy)
    return enriched


def _format_avg_value(avg: Any) -> str | None:
    if avg is None or avg == "":
        return None
    try:
        text = f"{float(avg):.3f}"
    except (TypeError, ValueError):
        return None
    return text[1:] if text.startswith("0.") else text


async def fetch_batter_vs_pitcher_stats(
    client: httpx.AsyncClient,
    batter_id: int,
    pitcher_id: int,
    *,
    season: int | None = None,
) -> dict[str, str | None]:
    """Batter vs pitcher AVG via MLB stats=vsPlayer (vsPitcher is no longer valid)."""
    season = season or date.today().year
    result: dict[str, str | None] = {
        "vsPitcherSeasonAvg": None,
        "vsPitcherCareerAvg": None,
    }

    async def _get(params: dict[str, Any]) -> list[dict[str, Any]]:
        async with _MLB_FETCH_SEM:
            resp = await client.get(
                f"{MLB_BASE}/people/{batter_id}/stats",
                params=params,
            )
        if resp.status_code != 200:
            return []
        stats = resp.json().get("stats") or []
        if not stats:
            return []
        return stats[0].get("splits") or []

    season_splits, career_splits = await asyncio.gather(
        _get(
            {
                "stats": "vsPlayer",
                "group": "hitting",
                "opposingPlayerId": pitcher_id,
                "sportId": 1,
            }
        ),
        _get(
            {
                "stats": "vsPlayerTotal",
                "group": "hitting",
                "opposingPlayerId": pitcher_id,
            }
        ),
    )

    season_key = str(season)
    for split in season_splits:
        if str(split.get("season") or "") != season_key:
            continue
        result["vsPitcherSeasonAvg"] = _format_avg_value((split.get("stat") or {}).get("avg"))
        break

    if career_splits:
        result["vsPitcherCareerAvg"] = _format_avg_value(
            (career_splits[0].get("stat") or {}).get("avg")
        )
    elif season_splits:
        # Aggregate season splits when total endpoint is empty.
        hits = 0
        at_bats = 0
        for split in season_splits:
            stat = split.get("stat") or {}
            hits += int(stat.get("hits") or 0)
            at_bats += int(stat.get("atBats") or 0)
        if at_bats > 0:
            result["vsPitcherCareerAvg"] = _format_avg_value(hits / at_bats)

    return result


async def enrich_batters_vs_pitcher(
    client: httpx.AsyncClient,
    batters: list[dict[str, Any]],
    pitcher_id: int | None,
) -> list[dict[str, Any]]:
    if not pitcher_id:
        return batters

    async def enrich_one(batter: dict[str, Any]) -> dict[str, Any]:
        copy = dict(batter)
        batter_id = copy.get("id")
        if batter_id:
            copy.update(
                await fetch_batter_vs_pitcher_stats(client, int(batter_id), int(pitcher_id))
            )
        at_bats = copy.get("atBats")
        hits = copy.get("hits")
        if at_bats is not None and hits is not None:
            copy["abHits"] = f"{int(at_bats)}-{int(hits)}"
        return copy

    return list(await asyncio.gather(*[enrich_one(batter) for batter in batters]))


async def fetch_matchup_starting_lineups(
    client: httpx.AsyncClient, matchup: dict[str, Any]
) -> dict[str, Any]:
    game_pk = matchup["gamePk"]
    feed = await fetch_game_feed(client, game_pk)
    box_teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})

    lineups: dict[str, Any] = {}
    for side_key in ("away", "home"):
        side_info = matchup[side_key]
        team_id = side_info["teamId"]
        batters = _parse_batters_from_team_box(box_teams.get(side_key, {}))
        source = "confirmed"
        source_date = matchup.get("date")
        if not batters:
            batters, source_date = await _fetch_previous_starting_lineup(client, team_id)
            source = "previous"
        opposing_key = "home" if side_key == "away" else "away"
        opposing_pitcher = matchup.get(opposing_key, {}).get("probablePitcher")
        pitcher_id = (opposing_pitcher or {}).get("id")
        if batters:
            batters = await enrich_batters_recent_form(client, batters)
            batters = await enrich_batters_vs_pitcher(client, batters, pitcher_id)
            batters = await enrich_batters_risp(client, batters)
        lineups[side_key] = {
            "teamName": side_info["teamName"],
            "source": source,
            "sourceDate": source_date,
            "opposingPitcher": opposing_pitcher,
            "batters": batters,
        }
    lineups["logicVersion"] = LINEUP_LOGIC_VERSION
    return lineups


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
    async with _MLB_FETCH_SEM:
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
    today_iso = date.today().isoformat()
    for day in data.get("dates", []):
        for game in day.get("games", []):
            state = game.get("status", {}).get("abstractGameState")
            if state not in UPCOMING_GAME_STATES:
                continue
            # Drop stale "Live/In Progress" leftovers from prior calendar days.
            official = game.get("officialDate") or day.get("date") or ""
            if state == "Live" and official and official < today_iso:
                continue
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
    async with _MLB_FETCH_SEM:
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
    client: httpx.AsyncClient,
    pitcher_id: int,
    pitcher_name: str,
    count: int = 10,
    *,
    fetch_limit: int = 40,
) -> dict[str, Any]:
    starts = await fetch_pitcher_starts(client, pitcher_id, fetch_limit)
    if not starts:
        empty_summary = summarize_pitcher_summary([], [])
        return {
            "pitcherId": pitcher_id,
            "pitcherName": pitcher_name,
            "games": [],
            "summary": empty_summary,
        }

    display_starts = starts[:count]
    away_starts = [split for split in starts if not split.get("isHome")][:10]
    home_starts = [split for split in starts if split.get("isHome")][:10]

    needed_splits: list[dict[str, Any]] = []
    seen_pks: set[int] = set()
    for split in display_starts + away_starts + home_starts:
        game_pk = split["game"]["gamePk"]
        if game_pk in seen_pks:
            continue
        seen_pks.add(game_pk)
        needed_splits.append(split)

    game_pks = [split["game"]["gamePk"] for split in needed_splits]
    linescores, starters_list, feeds = await asyncio.gather(
        asyncio.gather(*[fetch_linescore(client, pk) for pk in game_pks]),
        asyncio.gather(*[fetch_game_starters(client, pk) for pk in game_pks]),
        asyncio.gather(*[fetch_game_feed(client, pk) for pk in game_pks]),
    )

    row_by_pk: dict[int, dict[str, Any]] = {}
    for split, linescore, starters, feed in zip(needed_splits, linescores, starters_list, feeds):
        is_home = split.get("isHome", False)
        runs_allowed = first_five_runs_allowed(linescore, is_home)
        team_score, opponent_score = _scores_from_linescore(linescore, is_home)
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
        row_by_pk[split["game"]["gamePk"]] = {
            "date": split.get("date"),
            "gamePk": split["game"]["gamePk"],
            "opponent": team_name_zh(
                team_id=opponent_info.get("id"),
                english_name=opponent_info.get("name", "Unknown"),
            ),
            "opponentStarter": opponent_starter(starters, team_side),
            "isHome": split.get("isHome", False),
            "teamScore": team_score,
            "opponentScore": opponent_score,
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

    rows = [row_by_pk[split["game"]["gamePk"]] for split in needed_splits]
    display_rows = [
        row_by_pk[split["game"]["gamePk"]]
        for split in display_starts
        if split["game"]["gamePk"] in row_by_pk
    ]
    runs_list = [row["firstFiveRunsAllowed"] for row in display_rows]
    return {
        "pitcherId": pitcher_id,
        "pitcherName": pitcher_name,
        "games": display_rows,
        "_startPool": rows,
        "summary": summarize_pitcher_summary(display_rows, runs_list),
    }


async def analyze_team_scoring(
    client: httpx.AsyncClient, team_id: int, game_count: int = 10
) -> dict[str, Any]:
    pool = await fetch_recent_final_games(team_id, 50, client=client)
    if not pool:
        return {
            "teamId": team_id,
            "teamName": team_name_zh(team_id=team_id),
            "games": [],
            "_scoredPool": [],
            "summary": summarize_team_scoring([], []),
        }

    panel_games = pool[:game_count]
    away_targets = [game for game in pool if _team_side(game, team_id) == "away"][:10]
    home_targets = [game for game in pool if _team_side(game, team_id) == "home"][:10]

    needed_pks: list[int] = []
    seen_pks: set[int] = set()
    for game in panel_games + away_targets + home_targets:
        game_pk = game.get("gamePk")
        if game_pk in seen_pks:
            continue
        seen_pks.add(game_pk)
        needed_pks.append(game_pk)

    linescores, starters_list = await asyncio.gather(
        asyncio.gather(*[fetch_linescore(client, pk) for pk in needed_pks]),
        asyncio.gather(*[fetch_game_starters(client, pk) for pk in [g["gamePk"] for g in panel_games]]),
    )
    linescore_by_pk = dict(zip(needed_pks, linescores))

    def scored_innings_for(game: dict[str, Any]) -> list[int]:
        side = _team_side(game, team_id)
        linescore = linescore_by_pk[game["gamePk"]]
        scored: list[int] = []
        for inning in range(1, 10):
            runs = 0
            for inn in linescore.get("innings", []):
                if inn.get("num") == inning:
                    runs = inn.get(side, {}).get("runs", 0) or 0
                    break
            if runs > 0:
                scored.append(inning)
        return scored

    def build_row(
        game: dict[str, Any],
        *,
        starters: dict[str, Any] | None = None,
        include_scored: bool = False,
    ) -> dict[str, Any]:
        side = _team_side(game, team_id)
        opponent_info = game["teams"]["home" if side == "away" else "away"]["team"]
        is_home = side == "home"
        linescore = linescore_by_pk[game["gamePk"]]
        runs = first_five_runs(linescore, side)
        inning_one_runs = first_inning_runs(linescore, side)
        team_score = game["teams"][side].get("score")
        opponent_score = game["teams"]["away" if side == "home" else "home"].get("score")
        row = {
            "date": game.get("officialDate"),
            "gamePk": game.get("gamePk"),
            "opponent": team_name_zh(
                team_id=opponent_info.get("id"),
                english_name=opponent_info.get("name"),
            ),
            "opponentStarter": opponent_starter(starters, side) if starters else None,
            "teamStarter": starters.get(side) if starters else None,
            "isHome": is_home,
            "teamScore": team_score,
            "opponentScore": opponent_score,
            "firstInningRuns": inning_one_runs,
            "firstInningScored": inning_one_runs > 0,
            "firstFiveRuns": runs,
            "over15": runs > 1.5,
            "over25": runs > 2.5,
            "result": game["teams"][side].get("isWinner"),
        }
        if include_scored:
            row["scoredInnings"] = scored_innings_for(game)
        return row

    scored_pool: list[dict[str, Any]] = []
    pool_seen: set[int] = set()
    for game in away_targets + home_targets:
        game_pk = game.get("gamePk")
        if game_pk in pool_seen:
            continue
        pool_seen.add(game_pk)
        scored_pool.append(build_row(game, include_scored=True))

    rows = [
        build_row(game, starters=starters)
        for game, starters in zip(panel_games, starters_list)
    ]

    runs_list = [r["firstFiveRuns"] for r in rows]
    return {
        "teamId": team_id,
        "teamName": team_name_zh(team_id=team_id),
        "games": rows,
        "_scoredPool": scored_pool,
        "summary": summarize_team_scoring(rows, runs_list),
    }


async def fetch_inning_comparison(
    client: httpx.AsyncClient, team_id: int, *, game_count: int = 20
) -> dict[str, Any]:
    games = await fetch_recent_final_games(team_id, game_count, client=client)
    rows: list[dict[str, Any]] = []
    if games:
        linescores = await asyncio.gather(
            *[fetch_linescore(client, game["gamePk"]) for game in games]
        )
        for game, linescore in zip(games, linescores):
            side = _team_side(game, team_id)
            opp_side = "home" if side == "away" else "away"

            my_by_inning: list[int] = []
            opp_by_inning: list[int] = []
            for inning in linescore.get("innings", []):
                num = inning.get("num", 0)
                if num < 1 or num > 9:
                    continue
                while len(my_by_inning) < num:
                    my_by_inning.append(0)
                while len(opp_by_inning) < num:
                    opp_by_inning.append(0)
                my_by_inning[num - 1] = inning.get(side, {}).get("runs", 0) or 0
                opp_by_inning[num - 1] = inning.get(opp_side, {}).get("runs", 0) or 0

            scored_innings: list[int] = []
            allowed_innings: list[int] = []
            for index in range(9):
                inning = index + 1
                runs = my_by_inning[index] if index < len(my_by_inning) else 0
                runs_allowed = opp_by_inning[index] if index < len(opp_by_inning) else 0
                if runs > 0:
                    scored_innings.append(inning)
                if runs_allowed > 0:
                    allowed_innings.append(inning)

            rows.append(
                {
                    "scoredInnings": scored_innings,
                    "allowedInnings": allowed_innings,
                }
            )

    return build_inning_comparison(team_name_zh(team_id=team_id), rows)


async def analyze_matchup_a_table(focus_team_id: int) -> dict[str, Any]:
    async with httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
    ) as client:
        matchup = await fetch_next_matchup(client, focus_team_id)
        if not matchup:
            raise ValueError("找不到下一場比賽")
        away_table, home_table = await asyncio.gather(
            fetch_inning_comparison(client, matchup["away"]["teamId"]),
            fetch_inning_comparison(client, matchup["home"]["teamId"]),
        )
    return {"away": away_table, "home": home_table}


async def _build_side_panel(
    client: httpx.AsyncClient, side_info: dict[str, Any], game_count: int
) -> dict[str, Any]:
    scoring = await analyze_team_scoring(client, side_info["teamId"], game_count)
    pitcher_analysis = None
    probable = side_info.get("probablePitcher")
    if probable:
        pitcher_analysis = await analyze_pitcher_first_five_starts(
            client, probable["id"], probable["fullName"], game_count, fetch_limit=40
        )

    return {
        **scoring,
        "probablePitcher": probable,
        "pitcherAnalysis": pitcher_analysis,
    }


async def analyze_matchup(
    focus_team_id: int, game_count: int = 10, *, lite: bool = False
) -> dict[str, Any]:
    limits = (
        httpx.Limits(max_connections=8, max_keepalive_connections=4)
        if lite
        else httpx.Limits(max_connections=24, max_keepalive_connections=12)
    )
    async with httpx.AsyncClient(timeout=60.0, limits=limits) as client:
        matchup = await fetch_next_matchup(client, focus_team_id)
        if not matchup:
            raise ValueError("找不到下一場比賽")

        # playsport fallback for probable pitchers when statsapi returns None
        needs_away = not matchup["away"].get("probablePitcher")
        needs_home = not matchup["home"].get("probablePitcher")
        if needs_away or needs_home:
            try:
                from app.playsport_starters import fetch_playsport_starters
                ps_games = await fetch_playsport_starters(client)
                away_name = (matchup["away"].get("teamName") or "").lower()
                home_name = (matchup["home"].get("teamName") or "").lower()
                for pg in ps_games:
                    if pg.get("league") != "mlb":
                        continue
                    if (
                        pg.get("awayTeam", "").lower() in away_name
                        or away_name in pg.get("awayTeam", "").lower()
                    ) and (
                        pg.get("homeTeam", "").lower() in home_name
                        or home_name in pg.get("homeTeam", "").lower()
                    ):
                        if needs_away and pg.get("awayStarter"):
                            matchup["away"]["probablePitcher"] = {
                                "id": None, "fullName": pg["awayStarter"]
                            }
                        if needs_home and pg.get("homeStarter"):
                            matchup["home"]["probablePitcher"] = {
                                "id": None, "fullName": pg["homeStarter"]
                            }
                        break
            except Exception:
                pass

        away_id = matchup["away"]["teamId"]
        home_id = matchup["home"]["teamId"]
        if lite:
            # Render free tier: sequential panels, skip heavy a-table rebuild.
            away_panel = await _build_side_panel(client, matchup["away"], game_count)
            home_panel = await _build_side_panel(client, matchup["home"], game_count)
            starting_lineups = await fetch_matchup_starting_lineups(client, matchup)
            away_table = home_table = None
        else:
            away_panel, home_panel, away_table, home_table, starting_lineups = await asyncio.gather(
                _build_side_panel(client, matchup["away"], game_count),
                _build_side_panel(client, matchup["home"], game_count),
                fetch_inning_comparison(client, away_id),
                fetch_inning_comparison(client, home_id),
                fetch_matchup_starting_lineups(client, matchup),
            )
        situational = build_matchup_situational(away_panel, home_panel)
        away_panel = strip_panel_internals(away_panel)
        home_panel = strip_panel_internals(home_panel)

    result = {
        "focusTeamId": focus_team_id,
        "matchup": {
            "date": matchup["date"],
            "gameDate": matchup.get("gameDate"),
            "gamePk": matchup["gamePk"],
            "status": matchup["status"],
        },
        "away": away_panel,
        "home": home_panel,
        "startingLineups": starting_lineups,
        "situational": situational,
    }
    if away_table is not None and home_table is not None:
        result["aTable"] = {"away": away_table, "home": home_table}
    return result


async def analyze_team_first_five(team_id: int, game_count: int = 10) -> dict[str, Any]:
    games = await fetch_recent_final_games(team_id, game_count, client=client)
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
