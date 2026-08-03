"""Keep only cloud-essential matchup keys in cache.json (skip a-table blobs)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

KEEP = re.compile(r"^matchup:v\d+:\d+:10$")


def slim(path: Path) -> None:
    if not path.exists():
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return
    kept = {key: value for key, value in raw.items() if KEEP.match(key)}
    path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"slimmed {path.name}: {len(raw)} -> {len(kept)} keys")


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data"
    slim(root / "cache.json")
