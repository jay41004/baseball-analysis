"""Keep probable pitchers consistent across per-team matchup caches."""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_FINAL_STATUSES = {"final", "game over", "completed", "試合終了", "finished"}


def probable_pitchers_missing(data: dict) -> bool:
    matchup = data.get("matchup") or {}
    status = str(matchup.get("status") or "").strip().lower()
    if status in _FINAL_STATUSES:
        return False
    for side in ("away", "home"):
        name = ((data.get(side) or {}).get("probablePitcher") or {}).get("fullName")
        if not name:
            return True
    return False


def pitcher_name(panel: dict | None) -> str:
    return ((panel or {}).get("probablePitcher") or {}).get("fullName") or ""


def matchups_same_game(data: dict, prev_data: dict, league: str) -> bool:
    """True when two cached payloads refer to the same scheduled game."""
    prev_m = prev_data.get("matchup") or {}
    new_m = data.get("matchup") or {}
    if league == "mlb":
        old_pk, new_pk = prev_m.get("gamePk"), new_m.get("gamePk")
        return old_pk is not None and new_pk is not None and old_pk == new_pk
    if league in ("cpbl", "npb"):
        old_sno, new_sno = prev_m.get("gameSno"), new_m.get("gameSno")
        if old_sno is None or new_sno is None or old_sno != new_sno:
            return False
        if league == "cpbl":
            old_d = (prev_m.get("date") or "")[:10]
            new_d = (new_m.get("date") or "")[:10]
            if old_d and new_d and old_d != new_d:
                return False
        return True
    return False


def _pitcher_trust_score(pitcher: dict | None) -> int:
    if not pitcher:
        return 0
    name = (pitcher.get("fullName") or "").strip()
    if not name:
        return 0
    score = 1
    if pitcher.get("id"):
        score += 2
    return score


def merge_probable_pitchers_from_cache(
    data: dict,
    prev_data: dict | None,
    *,
    league: str,
    fill_only: bool = False,
) -> bool:
    """Keep known starters on same game when refresh would blank or regress them."""
    if not prev_data or not matchups_same_game(data, prev_data, league):
        return False
    changed = False
    for side in ("away", "home"):
        new_panel = data.get(side) or {}
        old_panel = prev_data.get(side) or {}
        if new_panel.get("teamId") != old_panel.get("teamId"):
            continue
        old_pitcher = old_panel.get("probablePitcher")
        new_pitcher = new_panel.get("probablePitcher")
        old_name = pitcher_name(old_panel).strip()
        new_name = pitcher_name(new_panel).strip()
        if not old_name:
            continue
        keep_old = False
        if not new_name:
            keep_old = True
        elif old_name != new_name:
            if fill_only:
                keep_old = True
            else:
                old_score = _pitcher_trust_score(old_pitcher)
                new_score = _pitcher_trust_score(new_pitcher)
                keep_old = new_score <= old_score
        if keep_old:
            new_panel["probablePitcher"] = copy.deepcopy(old_pitcher)
            data[side] = new_panel
            changed = True
    return changed


def restore_pitcher_analysis_after_header_patch(
    data: dict[str, Any],
    prev_snapshot: dict[str, Any] | None,
) -> bool:
    """Cloud-lite header patches must not leave starters without start rows."""
    changed = False
    for side in ("away", "home"):
        panel = data.get(side) or {}
        if (panel.get("pitcherAnalysis") or {}).get("games"):
            continue
        prev_panel = (prev_snapshot or {}).get(side) or {}
        name = pitcher_name(panel).strip()
        prev_name = pitcher_name(prev_panel).strip()
        prev_analysis = prev_panel.get("pitcherAnalysis")
        if not name or not isinstance(prev_analysis, dict):
            continue
        if prev_name and prev_name != name:
            continue
        games = prev_analysis.get("games") or []
        if not games:
            continue
        panel["pitcherAnalysis"] = copy.deepcopy(prev_analysis)
        data[side] = panel
        changed = True
    return changed


async def backfill_pitcher_analysis_from_pages(
    data: dict[str, Any],
    *,
    league: str,
    team_id: int,
    games: int,
) -> bool:
    """Copy pitcher start rows from GitHub Pages when Render lite stripped them."""
    from app.pages_mirror import fetch_pages_matchup

    pages_data = await fetch_pages_matchup(league, team_id, games)
    if not pages_data:
        return False
    changed = False
    for side in ("away", "home"):
        panel = data.get(side) or {}
        if (panel.get("pitcherAnalysis") or {}).get("games"):
            continue
        name = pitcher_name(panel).strip()
        if not name:
            continue
        peer = pages_data.get(side) or {}
        if pitcher_name(peer).strip() != name:
            continue
        analysis = peer.get("pitcherAnalysis")
        peer_games = (analysis or {}).get("games") or []
        if not peer_games:
            continue
        panel["pitcherAnalysis"] = copy.deepcopy(analysis)
        data[side] = panel
        changed = True
    return changed


def patch_probable_pitcher_header(
    panel: dict,
    new_pitcher: dict | None,
    *,
    game_changed: bool,
    force_refresh: bool = False,
) -> bool:
    """Apply header refresh rules for probablePitcher. Returns True if starter name changed."""
    old_name = pitcher_name(panel).strip()
    new_name = ((new_pitcher or {}).get("fullName") or "").strip()
    if game_changed:
        if new_name:
            changed = old_name != new_name
            panel["probablePitcher"] = new_pitcher
            if changed:
                panel.pop("pitcherAnalysis", None)
            return changed
        if old_name:
            panel["probablePitcher"] = None
            panel.pop("pitcherAnalysis", None)
            return True
        return False
    if new_name and not old_name:
        panel["probablePitcher"] = new_pitcher
        return False
    if force_refresh and new_name and old_name != new_name:
        panel["probablePitcher"] = new_pitcher
        panel.pop("pitcherAnalysis", None)
        return True
    return False


def restore_probable_pitchers_if_same_game(
    data: dict,
    prev_data: dict | None,
    *,
    league: str,
) -> bool:
    """When a full rebuild returns blank starters for the same game, keep cached names."""
    return merge_probable_pitchers_from_cache(
        data, prev_data, league=league, fill_only=False
    )


def same_matchup_date(left: dict, right: dict) -> bool:
    left_date = ((left.get("matchup") or {}).get("date") or "")[:10]
    right_date = ((right.get("matchup") or {}).get("date") or "")[:10]
    return bool(left_date and left_date == right_date)


def peer_same_game(left: dict, right: dict) -> bool:
    """True when two payloads are the same scheduled game (not just same date)."""
    left_m = left.get("matchup") or {}
    right_m = right.get("matchup") or {}
    left_pk, right_pk = left_m.get("gamePk"), right_m.get("gamePk")
    if left_pk is not None and right_pk is not None:
        return left_pk == right_pk
    left_sno, right_sno = left_m.get("gameSno"), right_m.get("gameSno")
    if left_sno is not None and right_sno is not None:
        left_d = (left_m.get("date") or "")[:10]
        right_d = (right_m.get("date") or "")[:10]
        return left_sno == right_sno and (not left_d or not right_d or left_d == right_d)
    return same_matchup_date(left, right)


def mirror_pitchers_from_peer_cache(
    data: dict,
    games: int,
    *,
    get_matchup: Callable[[int, int], dict[str, Any] | None],
) -> bool:
    """Copy probable pitchers from the opponent team's cache for the same game."""
    away_id = int((data.get("away") or {}).get("teamId") or 0)
    home_id = int((data.get("home") or {}).get("teamId") or 0)
    if not away_id or not home_id:
        return False

    changed = False
    for peer_id in (away_id, home_id):
        if not probable_pitchers_missing(data):
            break
        peer_entry = get_matchup(peer_id, games)
        if not peer_entry:
            continue
        peer_data = peer_entry.get("data") or {}
        if not peer_same_game(data, peer_data):
            continue
        for side in ("away", "home"):
            peer_pitcher = (peer_data.get(side) or {}).get("probablePitcher")
            peer_name = pitcher_name(peer_data.get(side))
            if not peer_name:
                continue
            panel = data.setdefault(side, {})
            if not pitcher_name(panel):
                panel["probablePitcher"] = copy.deepcopy(peer_pitcher)
                changed = True
    return changed


async def mirror_pitchers_to_peer(
    data: dict,
    games: int,
    focus_team_id: int,
    *,
    get_matchup: Callable[[int, int], dict[str, Any] | None],
    store_matchup: Callable[[int, int, dict[str, Any]], Awaitable[Any]],
) -> None:
    away_id = int((data.get("away") or {}).get("teamId") or 0)
    home_id = int((data.get("home") or {}).get("teamId") or 0)
    peer_id = home_id if focus_team_id == away_id else away_id
    if not peer_id:
        return

    peer_entry = get_matchup(peer_id, games)
    if not peer_entry:
        return

    peer_data = copy.deepcopy(peer_entry.get("data") or {})
    if not same_matchup_date(data, peer_data):
        return

    changed = False
    for side in ("away", "home"):
        pitcher = (data.get(side) or {}).get("probablePitcher")
        name = pitcher_name(data.get(side))
        if not name:
            continue
        panel = peer_data.setdefault(side, {})
        peer_name = pitcher_name(panel)
        if not peer_name:
            panel["probablePitcher"] = copy.deepcopy(pitcher)
            peer_data[side] = panel
            changed = True
        elif peer_name != name:
            # Never overwrite a peer's known starter with a conflicting name.
            logger.debug(
                "Skip peer pitcher mirror %s: peer has %s, focus has %s",
                side,
                peer_name,
                name,
            )
    if changed:
        await store_matchup(peer_id, games, peer_data)


async def sync_pitchers_on_read(
    team_id: int,
    games: int,
    entry: dict[str, Any],
    *,
    get_matchup: Callable[[int, int], dict[str, Any] | None],
    store_matchup: Callable[[int, int, dict[str, Any]], Awaitable[Any]],
    ensure_fresh: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    **ensure_kwargs: Any,
) -> dict[str, Any]:
    """Patch missing probable pitchers before returning API payload."""
    data = entry.get("data") or {}
    if not probable_pitchers_missing(data):
        return entry

    patched = copy.deepcopy(data)
    if mirror_pitchers_from_peer_cache(patched, games, get_matchup=get_matchup):
        await store_matchup(team_id, games, patched)
        await mirror_pitchers_to_peer(
            patched,
            games,
            team_id,
            get_matchup=get_matchup,
            store_matchup=store_matchup,
        )
        refreshed = get_matchup(team_id, games)
        if refreshed and not probable_pitchers_missing(refreshed.get("data") or {}):
            return refreshed

    if ensure_fresh is not None:
        try:
            return await ensure_fresh(team_id, games, entry, **ensure_kwargs)
        except Exception:
            logger.exception("ensure_fresh failed for team %s", team_id)
    return get_matchup(team_id, games) or entry


async def sync_all_peer_pitchers_for_league(
    team_ids: list[int],
    games: int,
    *,
    get_matchup: Callable[[int, int], dict[str, Any] | None],
    store_matchup: Callable[[int, int, dict[str, Any]], Awaitable[Any]],
) -> int:
    """Startup repair: copy pitchers across caches for the same game."""
    fixed = 0
    for team_id in team_ids:
        entry = get_matchup(team_id, games)
        if not entry:
            continue
        data = entry.get("data") or {}
        if not probable_pitchers_missing(data):
            continue
        patched = copy.deepcopy(data)
        if mirror_pitchers_from_peer_cache(patched, games, get_matchup=get_matchup):
            await store_matchup(team_id, games, patched)
            await mirror_pitchers_to_peer(
                patched,
                games,
                team_id,
                get_matchup=get_matchup,
                store_matchup=store_matchup,
            )
            fixed += 1
    return fixed
