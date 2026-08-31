"""Prefer previous gh-pages league JSON when a new build is thinner."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def _count_matchups(league_dir: Path) -> int:
    if not league_dir.is_dir():
        return 0
    return len(list(league_dir.glob("matchup_*_10.json")))


def _matchup_tuple(path: Path) -> tuple[str, int, int]:
    """Sort key: newer game date wins; then richer payload."""
    if not path.is_file():
        return ("", 0, 0)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ("", 0, 0)
    date = str((data.get("matchup") or {}).get("date") or "")[:10]
    richness = 0
    for side in ("away", "home"):
        panel = data.get(side) or {}
        if panel.get("probablePitcher"):
            richness += 2
        if panel.get("pitcherAnalysis"):
            richness += 4
        richness += min(len(panel.get("games") or []), 10)
        lineups = panel.get("lineups") or data.get("lineups") or {}
        batters = (lineups.get(side) or {}).get("batters") or []
        if len(batters) >= 7:
            richness += 3
    return (date, richness, _count_matchups(path.parent))


def _merge_league_files(prev_dir: Path, new_dir: Path) -> None:
    if not prev_dir.is_dir() or not new_dir.is_dir():
        return
    for new_file in new_dir.glob("matchup_*_10.json"):
        prev_file = prev_dir / new_file.name
        if not prev_file.is_file():
            continue
        if _matchup_tuple(prev_file) > _matchup_tuple(new_file):
            print(f"  keep prev {new_file.name}")
            shutil.copy2(prev_file, new_file)


def _copy_league(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.iterdir():
        target = dst / path.name
        if path.is_file():
            shutil.copy2(path, target)


def merge(prev_root: Path, new_root: Path) -> None:
    prev_data = prev_root / "data"
    new_data = new_root / "data"
    if not new_data.is_dir():
        raise SystemExit(f"missing new data dir: {new_data}")

    for league, min_keep in (("mlb", 20), ("npb", 8), ("cpbl", 4)):
        prev_n = _count_matchups(prev_data / league)
        new_n = _count_matchups(new_data / league)
        if prev_n > new_n and prev_n >= min_keep:
            print(f"KEEP previous {league}: new={new_n} prev={prev_n}")
            _copy_league(prev_data / league, new_data / league)
        else:
            print(f"USE new {league}: new={new_n} prev={prev_n}")
            _merge_league_files(prev_data / league, new_data / league)

    # Refresh meta counts from whatever we kept.
    meta_path = new_data / "meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["mlbTeamsCached"] = _count_matchups(new_data / "mlb")
    meta["npbTeamsCached"] = _count_matchups(new_data / "npb")
    meta["cpblTeamsCached"] = _count_matchups(new_data / "cpbl")
    meta["cacheReady"] = (
        meta["mlbTeamsCached"] >= 20
        and meta["npbTeamsCached"] >= 8
        and meta["cpblTeamsCached"] >= 4
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("merged meta", meta)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: merge_keep_best.py <prev_docs> <new_docs>")
    merge(Path(sys.argv[1]), Path(sys.argv[2]))
