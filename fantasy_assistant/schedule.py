"""The NBA season schedule: which teams play on which days.

Downloaded from NBA.com once a day and cached in data/, so it picks up
postponed games and works again next season with no manual updates.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_AGE = 24 * 60 * 60

# Without a status from Sleeper's schedule, a game today counts as started
# from 7pm (most tip-offs are 7pm or later; some are as late as 10:30pm).
FALLBACK_TIPOFF_HOUR = 19
LOCK_BEFORE_TIPOFF = "plays today, lock before tip-off"

# Sleeper sometimes uses short team codes; NBA.com uses these.
TEAM_ALIASES = {"NO": "NOP", "GS": "GSW", "SA": "SAS", "NY": "NYK",
                "PHO": "PHX", "UTAH": "UTA", "WSH": "WAS"}


@dataclass(frozen=True)
class Game:
    game_id: str
    day: date
    home: str
    away: str

    def opponent_of(self, team: str) -> str:
        return f"vs {self.away}" if team == self.home else f"@{self.home}"


def nba_team(code: str | None) -> str | None:
    return TEAM_ALIASES.get(code, code) if code else None


def _parse_day(row: dict) -> date | None:
    """NBA.com dates look like '2026-10-27T00:00:00Z' (Eastern date)."""
    raw = row.get("gameDateEst") or row.get("gameDate") or ""
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_games(rows: list[dict]) -> list[Game]:
    """Raw schedule rows -> one Game per game id (rows can repeat per broadcaster)."""
    games: dict[str, Game] = {}
    for row in rows:
        day = _parse_day(row)
        gid = row.get("gameId")
        home, away = row.get("homeTeam_teamTricode"), row.get("awayTeam_teamTricode")
        if gid and day and home and away and gid not in games:
            games[gid] = Game(gid, day, home, away)
    return sorted(games.values(), key=lambda g: (g.day, g.game_id))


def season_games(season: str) -> list[Game]:
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / f"schedule_{season}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < MAX_AGE:
        rows = json.loads(cache.read_text(encoding="utf-8"))
    else:
        from nba_api.stats.endpoints import scheduleleaguev2

        rows = scheduleleaguev2.ScheduleLeagueV2(season=season, timeout=60) \
            .get_normalized_dict()["SeasonGames"]
        slim = [{k: r.get(k) for k in ("gameId", "gameDateEst", "gameDate",
                                       "homeTeam_teamTricode", "awayTeam_teamTricode")}
                for r in rows]
        cache.write_text(json.dumps(slim), encoding="utf-8")
        rows = slim
    return parse_games(rows)


def fantasy_week(today: date) -> list[date]:
    """Monday through Sunday of the week containing `today`."""
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def team_games(games: list[Game], team: str, days: list[date]) -> dict[date, Game]:
    """A team's games on the given days, keyed by day."""
    wanted = set(days)
    return {g.day: g for g in games if g.day in wanted and team in (g.home, g.away)}


def still_to_come(day: date, now: datetime, status: str | None) -> bool:
    """Whether a game on `day` hasn't started yet at `now`.

    A game today is still to come while Sleeper's schedule says pre_game
    (it flips to in_game within a few minutes of tip-off). With no status
    (Sleeper's schedule couldn't be read, or doesn't list the game yet),
    the old rule applies: today's games count as started from 7pm.
    """
    if day != now.date():
        return day > now.date()
    if status is None:
        return now.hour < FALLBACK_TIPOFF_HOUR
    return status == "pre_game"


def games_to_come(slate: dict[date, Game], team: str, now: datetime,
                  today_statuses: dict[str, str] | None) -> dict[date, Game]:
    """The games in a team's slate that haven't started yet.

    `today_statuses` is {team: status} for today's games (from
    sleeper_stats.team_statuses), or None if it couldn't be read.
    """
    status = (today_statuses or {}).get(team)
    return {d: g for d, g in slate.items() if still_to_come(d, now, status)}


def tipoff_note(to_come: dict[date, Game], today: date) -> str:
    """The warning for a player whose next game is today and hasn't started:
    a score in hand must be locked before tip-off, or that game replaces it."""
    return LOCK_BEFORE_TIPOFF if today in to_come else ""
