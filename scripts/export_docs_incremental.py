"""Update docs/data + docs/static from local cache without wiping docs/."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"
DEFAULT_GAMES = 10

sys.path.insert(0, str(ROOT))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fix_html(html: str, *, depth: int) -> str:
    prefix = "../" * depth
    html = html.replace('href="/static/', f'href="{prefix}static/')
    html = html.replace('src="/static/', f'src="{prefix}static/')
    html = html.replace('href="/"', f'href="{prefix}"')
    html = html.replace('href="/npb"', f'href="{prefix}npb/"')
    html = html.replace('href="/cpbl"', f'href="{prefix}cpbl/"')
    inject = '<script>window.SITE_STATIC=true;</script>\n  <script src="'
    html = html.replace('  <script src="', inject, 1)
    html = html.replace("資料連線雲端即時更新", "GitHub 定時自動更新，不依賴本機")
    html = html.replace("（GitHub 為靜態快照）", "（每幾小時自動刷新）")
    return html


def sync_assets() -> None:
    dst = DOCS / "static"
    dst.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "static").iterdir():
        target = dst / path.name
        if path.is_file():
            shutil.copy2(path, target)
    pages = [
        (ROOT / "templates" / "index.html", DOCS / "index.html", 0),
        (ROOT / "templates" / "npb.html", DOCS / "npb" / "index.html", 1),
        (ROOT / "templates" / "cpbl.html", DOCS / "cpbl" / "index.html", 1),
    ]
    for src, dst_path, depth in pages:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_text(_fix_html(src.read_text(encoding="utf-8"), depth=depth), encoding="utf-8")


def export_league(module: str, league: str) -> int:
    if league == "mlb":
        from app.cache import get_matchup, load_from_disk, wrap_matchup_response
        from app.mlb_service import fetch_teams

        load_from_disk()
        teams = __import__("asyncio").run(fetch_teams())
    elif league == "npb":
        from app.npb_cache import get_matchup, load_from_disk, wrap_matchup_response
        from app.npb_service import fetch_npb_teams

        load_from_disk()
        teams = __import__("asyncio").run(fetch_npb_teams())
    else:
        from app.cpbl_cache import get_matchup, load_from_disk, wrap_matchup_response
        from app.cpbl_service import fetch_cpbl_teams

        load_from_disk()
        teams = __import__("asyncio").run(fetch_cpbl_teams())

    _write_json(DATA / league / "teams.json", teams)
    exported = 0
    for team in teams:
        tid = team["id"]
        entry = get_matchup(tid, DEFAULT_GAMES)
        if entry:
            payload = wrap_matchup_response(entry, from_cache=True)
            _write_json(DATA / league / f"matchup_{tid}_{DEFAULT_GAMES}.json", payload)
            exported += 1
    print(f"{league.upper()}: exported {exported}/{len(teams)}")
    return exported


def write_meta() -> None:
    from app.cache import CACHE_VERSION as MLB_V, cached_team_count as mlb_c, load_from_disk as load_mlb
    from app.cpbl_cache import CACHE_VERSION as CPBL_V, cached_team_count as cpbl_c, load_from_disk as load_cpbl
    from app.npb_cache import CACHE_VERSION as NPB_V, cached_team_count as npb_c, load_from_disk as load_npb

    load_mlb()
    load_npb()
    load_cpbl()
    _write_json(
        DATA / "meta.json",
        {
            "mode": "static",
            "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
            "mlbCacheVersion": MLB_V,
            "npbCacheVersion": NPB_V,
            "cpblCacheVersion": CPBL_V,
            "mlbTeamsCached": mlb_c(DEFAULT_GAMES),
            "npbTeamsCached": npb_c(DEFAULT_GAMES),
            "cpblTeamsCached": cpbl_c(DEFAULT_GAMES),
            "cacheReady": True,
            "note": "Refreshed locally for GitHub Pages",
        },
    )


def main() -> None:
    sync_assets()
    export_league("app.cache", "mlb")
    export_league("app.npb_cache", "npb")
    export_league("app.cpbl_cache", "cpbl")
    from scripts.export_slates import export_slates

    export_slates(DATA)
    write_meta()
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Done: {DOCS}")


if __name__ == "__main__":
    main()
