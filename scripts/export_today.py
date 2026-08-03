import json
from datetime import date
from pathlib import Path

schedule = json.loads(Path("data/cpbl_schedule.json").read_text(encoding="utf-8"))
today = date.today().isoformat()
games = [g for g in schedule["games"] if g.get("date") == today]
Path("_today_games.json").write_text(json.dumps(games, ensure_ascii=False, indent=2), encoding="utf-8")
