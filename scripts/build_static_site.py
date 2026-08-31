"""Build GitHub Pages static site from local cache (no server, no 502)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"
DEFAULT_GAMES = 10


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
    # Phone path uses Actions-refreshed static JSON (independent of Render).
    html = html.replace(
        "資料連線雲端即時更新",
        "GitHub 定時自動更新，不依賴本機",
    )
    html = html.replace("（GitHub 為靜態快照）", "（每幾小時自動刷新）")
    return html


def _copy_assets() -> None:
    if DOCS.exists():
        shutil.rmtree(DOCS)
    DOCS.mkdir()
    shutil.copytree(ROOT / "static", DOCS / "static")

    pages = [
        (ROOT / "templates" / "index.html", DOCS / "index.html", 0),
        (ROOT / "templates" / "npb.html", DOCS / "npb" / "index.html", 1),
        (ROOT / "templates" / "cpbl.html", DOCS / "cpbl" / "index.html", 1),
    ]
    for src, dst, depth in pages:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(_fix_html(src.read_text(encoding="utf-8"), depth=depth), encoding="utf-8")


def _export_mlb() -> None:
    from app.cache import DEFAULT_GAMES, cached_team_count, get_matchup, load_from_disk, wrap_matchup_response
    from app.mlb_service import fetch_teams

    load_from_disk()
    teams = __import__("asyncio").run(fetch_teams())
    _write_json(DATA / "mlb" / "teams.json", teams)

    for team in teams:
        tid = team["id"]
        entry = get_matchup(tid, DEFAULT_GAMES)
        if entry:
            payload = wrap_matchup_response(entry, from_cache=True)
            _write_json(DATA / "mlb" / f"matchup_{tid}_{DEFAULT_GAMES}.json", payload)

    print(f"MLB: {len(teams)} teams, cache hits {cached_team_count(DEFAULT_GAMES)}")


def _export_npb() -> None:
    from app.npb_cache import DEFAULT_GAMES, cached_team_count, get_matchup, load_from_disk, wrap_matchup_response
    from app.npb_service import fetch_npb_teams

    load_from_disk()
    teams = __import__("asyncio").run(fetch_npb_teams())
    _write_json(DATA / "npb" / "teams.json", teams)

    for team in teams:
        tid = team["id"]
        entry = get_matchup(tid, DEFAULT_GAMES)
        if entry:
            payload = wrap_matchup_response(entry, from_cache=True)
            _write_json(DATA / "npb" / f"matchup_{tid}_{DEFAULT_GAMES}.json", payload)

    print(f"NPB: {len(teams)} teams, cache hits {cached_team_count(DEFAULT_GAMES)}")


def _export_cpbl() -> None:
    from app.cpbl_cache import DEFAULT_GAMES, cached_team_count, get_matchup, load_from_disk, wrap_matchup_response
    from app.cpbl_service import fetch_cpbl_teams

    load_from_disk()
    teams = __import__("asyncio").run(fetch_cpbl_teams())
    _write_json(DATA / "cpbl" / "teams.json", teams)

    for team in teams:
        tid = team["id"]
        entry = get_matchup(tid, DEFAULT_GAMES)
        if entry:
            payload = wrap_matchup_response(entry, from_cache=True)
            _write_json(DATA / "cpbl" / f"matchup_{tid}_{DEFAULT_GAMES}.json", payload)

    print(f"CPBL: {len(teams)} teams, cache hits {cached_team_count(DEFAULT_GAMES)}")


def _write_meta() -> None:
    from app.cache import CACHE_VERSION as MLB_V, cached_team_count as mlb_c, load_from_disk as load_mlb
    from app.cpbl_cache import CACHE_VERSION as CPBL_V, cached_team_count as cpbl_c, load_from_disk as load_cpbl
    from app.npb_cache import CACHE_VERSION as NPB_V, cached_team_count as npb_c, load_from_disk as load_npb

    from datetime import datetime, timezone

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
            "note": "Refreshed by GitHub Actions on a schedule",
        },
    )


def main() -> None:
    sys.path.insert(0, str(ROOT))
    _copy_assets()
    _export_mlb()
    _export_npb()
    _export_cpbl()
    from scripts.export_slates import export_slates

    export_slates(DATA)
    _write_meta()
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Static site ready: {DOCS}")


if __name__ == "__main__":
    main()
