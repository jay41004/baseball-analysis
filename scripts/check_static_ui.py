"""Verify GitHub Pages UI bundle has required merge/pick/lineup guards."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_MARKERS: dict[str, list[str]] = {
    "static/api_utils.js": [
        "mergeLiveMatchupHeader",
        "fetchStaticMatchupWithLiveHeader",
        "pickForTeam",
        "liveMatchupUrl",
        "shouldPreferBackgroundRefresh",
    ],
    "static/lineup_loader.js": [
        "cancelPending",
        "resolvePick",
        "canKeepShowingLineups",
    ],
    "static/app.js": [
        "fetchStaticMatchupWithLiveHeader",
        "pickForTeam",
    ],
    "static/npb.js": [
        "fetchStaticMatchupWithLiveHeader",
        "pickForTeam",
    ],
    "static/cpbl.js": [
        "fetchStaticMatchupWithLiveHeader",
        "pickForTeam",
    ],
}

FORBIDDEN_MARKERS: dict[str, list[str]] = {
    "static/npb.js": ["data = live.data"],
}


def verify(root: Path) -> list[str]:
    errors: list[str] = []
    for rel, markers in REQUIRED_MARKERS.items():
        path = root / rel
        if not path.is_file():
            errors.append(f"missing file: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                errors.append(f"{rel}: missing `{marker}`")
    for rel, markers in FORBIDDEN_MARKERS.items():
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                errors.append(f"{rel}: forbidden `{marker}` (use mergeLiveMatchupHeader)")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repo root or docs/ (will check static/ or docs/static/)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if (root / "static" / "api_utils.js").is_file():
        check_root = root
    elif (root / "docs" / "static" / "api_utils.js").is_file():
        check_root = root / "docs"
    else:
        raise SystemExit(f"cannot find static/api_utils.js under {root}")

    errors = verify(check_root)
    if errors:
        print("STATIC UI CHECK FAILED:")
        for err in errors:
            print(f"  - {err}")
        raise SystemExit(1)
    print(f"STATIC UI OK ({check_root})")


if __name__ == "__main__":
    main()
