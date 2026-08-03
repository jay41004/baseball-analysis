"""Verify CPBL A-table scored/allowed vs raw box innings."""
from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.cpbl_service import TEAM_BY_ID, CpblClient, _box_has_inning_data, build_inning_comparison


async def main() -> None:
    team_id = next(i for i, t in TEAM_BY_ID.items() if "兄弟" in t["nameZh"])
    client = CpblClient()
    try:
        schedule = await client.fetch_schedule_pool()
        finished = [
            g
            for g in schedule
            if g.get("status") == "Final"
            and team_id in {g["awayTeamId"], g["homeTeamId"]}
            and g.get("gameSno") is not None
        ]
        finished.sort(key=lambda g: g.get("date", ""), reverse=True)
        print("finals available", len(finished))

        rows = []
        for meta in finished[:30]:
            if len(rows) >= 20:
                break
            box = await client.fetch_box(int(meta["gameSno"]), int(meta.get("year") or 2026))
            if not box or not _box_has_inning_data(box):
                print("SKIP no innings", meta.get("date"), meta.get("gameSno"))
                continue
            # prefer schedule team ids
            is_home = meta["homeTeamId"] == team_id
            side = "home" if is_home else "away"
            opp = "away" if is_home else "home"
            # also check box team ids
            box_home = box.get("homeTeamId")
            print(
                meta.get("date"),
                "sno",
                meta.get("gameSno"),
                "home?" ,
                is_home,
                "boxHomeId",
                box_home,
                "my",
                box[f"{side}Innings"],
                "opp",
                box[f"{opp}Innings"],
                "score",
                meta.get("awayScore"),
                "-",
                meta.get("homeScore"),
            )
            scored = [i + 1 for i, r in enumerate(box[f"{side}Innings"][:9]) if r > 0]
            allowed = [i + 1 for i, r in enumerate(box[f"{opp}Innings"][:9]) if r > 0]
            rows.append({"scoredInnings": scored, "allowedInnings": allowed})

        data = build_inning_comparison("中信兄弟", rows)
        for key in ("recent5", "recent10", "recent20"):
            b = data[key]
            print(key, "n=", b["gameCount"], "s1=", b["scoredCounts"]["1"], "a1=", b["allowedCounts"]["1"])
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
