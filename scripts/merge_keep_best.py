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
