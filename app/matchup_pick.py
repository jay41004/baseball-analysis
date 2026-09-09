"""Match a user-picked slate row to cached / live matchup payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExpectedMatchup:
    date: str | None = None
    away_id: int | None = None
    home_id: int | None = None
    game_pk: int | None = None

    @classmethod
    def from_query(
        cls,
        *,
        expected_date: str | None,
        expected_away: int | None,
        expected_home: int | None,
        expected_game_pk: int | None = None,
    ) -> ExpectedMatchup | None:
        date = (expected_date or "").strip()[:10] or None
        game_pk = int(expected_game_pk) if expected_game_pk else None
        if not date and not game_pk:
            return None
        return cls(
            date=date,
            away_id=int(expected_away) if expected_away else None,
            home_id=int(expected_home) if expected_home else None,
            game_pk=game_pk,
        )

    def _date_matches_row(self, game: dict[str, Any]) -> bool:
        if not self.date:
            return True
        gd = str(game.get("date") or "")[:10]
        od = str(game.get("officialDate") or "")[:10]
        return self.date in {gd, od}

    def matches_payload(self, payload: dict[str, Any] | None) -> bool:
        if not payload:
            return not (self.date or self.game_pk)
        matchup = payload.get("matchup") or {}
        if self.game_pk and matchup.get("gamePk") != self.game_pk:
            return False
        away = int((payload.get("away") or {}).get("teamId") or 0)
        home = int((payload.get("home") or {}).get("teamId") or 0)
        if self.away_id and self.home_id and {away, home} != {self.away_id, self.home_id}:
            return False
        if not self.date:
            return True
        md = str(matchup.get("date") or "")[:10]
        od = str(matchup.get("officialDate") or "")[:10]
        return self.date in {md, od}

    def matches_cache_entry(self, entry: dict[str, Any] | None) -> bool:
        if not entry:
            return not self.date
        return self.matches_payload(entry.get("data") or {})

    def matches_schedule_row(self, game: dict[str, Any]) -> bool:
        if self.game_pk and game.get("gamePk") and int(game["gamePk"]) != self.game_pk:
            return False
        if not self._date_matches_row(game):
            return False
        away = int(game.get("awayTeamId") or 0)
        home = int(game.get("homeTeamId") or 0)
        if self.away_id and self.home_id:
            return {away, home} == {self.away_id, self.home_id}
        return True

    @classmethod
    def from_cache_entry(cls, entry: dict[str, Any] | None) -> ExpectedMatchup | None:
        """Build pick from stored matchup so background jobs stay on the same game."""
        if not entry:
            return None
        data = entry.get("data") or {}
        matchup = data.get("matchup") or {}
        date = str(matchup.get("date") or "")[:10] or None
        if not date:
            return None
        away_id = int((data.get("away") or {}).get("teamId") or 0) or None
        home_id = int((data.get("home") or {}).get("teamId") or 0) or None
        return cls(date=date, away_id=away_id, home_id=home_id)
