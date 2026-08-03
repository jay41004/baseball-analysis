import json
from datetime import date
from pathlib import Path

schedule = json.loads(Path("data/cpbl_schedule.json").read_text(encoding="utf-8"))
today = date.today().isoformat()
games = [g for g in schedule["games"] if g.get("date") == today]
print(f"today={today} games={len(games)}")
for g in games:
    print(
        f"{g.get('awayNameZh')} @ {g.get('homeNameZh')} | "
        f"客:{g.get('awayProbablePitcher') or '尚未公布'} | "
        f"主:{g.get('homeProbablePitcher') or '尚未公布'} | "
        f"status={g.get('status')}"
    )
