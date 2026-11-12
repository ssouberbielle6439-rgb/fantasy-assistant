"""Game logs from Sleeper's per-game stats (the main source; NBA.com is the fallback).

These are Sleeper's own box scores, keyed by Sleeper player id, so there's no
name matching, and they include technical fouls, which NBA.com's game logs
don't. Scored with scoring.py they match Sleeper's fantasy points to the
decimal (checked 11/02).

The URLs are unofficial and undocumented, so anything unexpected raises
SleeperStatsUnavailable and the caller falls back to NBA.com.

Two rules keep the numbers trustworthy:
- A game counts only once Sleeper's schedule says it's `complete`. Mid-game,
  a player who hasn't checked in yet has empty stats, the same as a player
  who sits out, so empty stats only mean "didn't play" after that.
- For each game we keep the newest version of the stat line, by its
  `updated_at` time. Sleeper's copies are cached and can come back older than
  what we already have; this way a stale copy can't overwrite newer data, and
  stat corrections made days later still come in.

Results are cached in data/gamelogs/ next to the NBA.com logs.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import requests

BASE_URL = "https://api.sleeper.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "gamelogs"
CURRENT_SEASON_MAX_AGE = 60 * 60  # refresh this season's games every hour
TIMEOUT = 20

# Our box-score key -> Sleeper's stat key. Our keys follow nba_api naming,
# which is what scoring.py reads, so both sources score the same way.
_STAT_NAMES = {
    "pts": "pts", "reb": "reb", "oreb": "oreb", "ast": "ast", "stl": "stl",
    "blk": "blk", "tov": "to", "pf": "pf", "fgm": "fgm", "fga": "fga",
    "fg3m": "tpm", "ftm": "ftm", "fta": "fta", "min": "min",
    "tf": "tf", "ff": "ff",
}

# The parts of each game we keep in the cache (Sleeper also sends the
# player's profile with every game, which we don't need).
_KEEP = ("game_id", "date", "team", "opponent", "is_away_team", "updated_at", "stats")


class SleeperStatsUnavailable(Exception):
    """Sleeper couldn't be reached or sent something we don't recognize."""


def sleeper_season(season: str) -> str:
    """NBA.com's season name -> Sleeper's: '2026-27' -> '2026'."""
    return season[:4]


def _get_json(path: str):
    try:
        resp = requests.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as err:
        raise SleeperStatsUnavailable("couldn't reach Sleeper") from err


# ---------- Reading Sleeper's responses (pure functions, easy to test) ----------

def parse_player_games(data) -> list[dict]:
    """One-player response ({"1": [game, ...], "2": [...], ...}) -> his games.

    The dict's keys are fantasy weeks. Weeks he had no games are empty lists.
    """
    if not isinstance(data, dict):
        raise SleeperStatsUnavailable("unexpected response from Sleeper")
    games = []
    for week_games in data.values():
        if not isinstance(week_games, list):
            raise SleeperStatsUnavailable("unexpected response from Sleeper")
        for game in week_games:
            if not (isinstance(game, dict) and game.get("game_id") and game.get("date")
                    and isinstance(game.get("stats") or {}, dict)):
                raise SleeperStatsUnavailable("unexpected response from Sleeper")
            games.append({k: game.get(k) for k in _KEEP})
    return games


def parse_statuses(data) -> dict[str, str]:
    """Schedule response (a list of games) -> {game_id: status}.

    Statuses seen: pre_game, in_game, complete, postponed, canceled.
    """
    if not isinstance(data, list):
        raise SleeperStatsUnavailable("unexpected response from Sleeper")
    statuses = {}
    for game in data:
        if not (isinstance(game, dict) and game.get("game_id") and game.get("status")):
            raise SleeperStatsUnavailable("unexpected response from Sleeper")
        statuses[game["game_id"]] = game["status"]
    return statuses


def merge_games(saved: dict[str, dict], fresh: list[dict]) -> dict[str, dict]:
    """Combine cached and freshly downloaded games: {game_id: game}.

    For each game, the version with the newest `updated_at` wins, so an older
    cached copy from Sleeper never replaces a newer line we already have.
    """
    merged = dict(saved)
    for game in fresh:
        old = merged.get(game["game_id"])
        if old is None or (game.get("updated_at") or 0) > (old.get("updated_at") or 0):
            merged[game["game_id"]] = game
    return merged


def parse_teams(data) -> dict[str, list[str]]:
    """Schedule response -> {game_id: [date, home team, away team]}.

    Games whose teams aren't set yet (NBA Cup knockout games) are left out.
    """
    teams = {}
    for game in data:
        home = (game.get("home") or {}).get("team")
        away = (game.get("away") or {}).get("team")
        if game.get("game_id") and game.get("date") and home and away:
            teams[game["game_id"]] = [game["date"], home, away]
    return teams


# How far along a game is. A status never moves back down this list.
# Postponed and canceled count as 0, so they can replace pre_game but never
# a game that has started.
_STAGE = {"pre_game": 0, "in_game": 1, "complete": 2}


def merge_statuses(saved: dict[str, str], fresh: dict[str, str]) -> dict[str, str]:
    """Statuses only move forward: pre_game -> in_game -> complete.

    On 11/02 a stale copy of the schedule showed three finished games as
    in_game again for two minutes. A stale copy could just as well show a
    game that has tipped off as pre_game, which would make it look like it's
    still to come, so a fresh status never replaces one that's further along.
    """
    merged = dict(saved)
    for game_id, status in fresh.items():
        if _STAGE.get(status, 0) >= _STAGE.get(saved.get(game_id), 0):
            merged[game_id] = status
    return merged


def statuses_by_team(statuses: dict[str, str], teams: dict[str, list[str]],
                     day: date) -> dict[str, str]:
    """{team: status} for the games on `day`. Team codes use NBA.com's spelling
    (schedule.nba_team), the same as the week's schedule."""
    from .schedule import nba_team

    by_team = {}
    for game_id, (game_day, home, away) in teams.items():
        if game_day == day.isoformat() and game_id in statuses:
            by_team[nba_team(home)] = by_team[nba_team(away)] = statuses[game_id]
    return by_team


def to_box(game: dict) -> dict:
    """Sleeper game -> the box-score dict scoring.py expects (same as NBA.com's)."""
    stats = game.get("stats") or {}
    box = {ours: stats.get(theirs) or 0 for ours, theirs in _STAT_NAMES.items()}
    # Same date and matchup format as NBA.com's game logs ("Oct 29, 2026",
    # "POR vs. MEM"), so the code reading them works with either source.
    box["game_date"] = f"{date.fromisoformat(game['date']):%b %d, %Y}"
    box["matchup"] = f"{game.get('team')} {'@' if game.get('is_away_team') else 'vs.'} " \
                     f"{game.get('opponent')}"
    box["game_id"] = game["game_id"]
    return box


def played_games(games: dict[str, dict], statuses: dict[str, str]) -> list[dict]:
    """Box scores of the finished games he played in, most recent first.

    A game that isn't complete yet is left out, even if it has partial stats.
    A complete game with empty stats means he didn't play, so it's left out too.
    """
    finished = [g for g in games.values() if statuses.get(g["game_id"]) == "complete"]
    played = [g for g in finished if g.get("stats")]
    played.sort(key=lambda g: g["date"], reverse=True)
    return [to_box(g) for g in played]


# ---------- Downloading and caching ----------

def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _age(path: Path) -> float:
    return time.time() - path.stat().st_mtime


def game_statuses(season: str, is_current: bool,
                  max_age: float = CURRENT_SEASON_MAX_AGE) -> dict[str, str]:
    """{game_id: status} for every game in a season ("2026-27"), cached.

    Each download also saves who plays each game (sleeper_teams_<season>.json),
    which team_statuses needs to find a team's game today.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = DATA_DIR / f"sleeper_schedule_{season}.json"
    saved = _load(cache)
    if saved and (not is_current or _age(cache) < max_age):
        return saved
    data = _get_json(f"/schedule/nba/regular/{sleeper_season(season)}")
    merged = merge_statuses(saved, parse_statuses(data))
    (DATA_DIR / f"sleeper_teams_{season}.json").write_text(json.dumps(parse_teams(data)),
                                                            encoding="utf-8")
    cache.write_text(json.dumps(merged), encoding="utf-8")
    return merged


def team_statuses(season: str, day: date, max_age: float = 60) -> dict[str, str]:
    """{team: status} for the games on `day` (normally today), at most
    `max_age` seconds old. Raises SleeperStatsUnavailable if the schedule
    can't be downloaded."""
    statuses = game_statuses(season, is_current=True, max_age=max_age)
    teams_cache = DATA_DIR / f"sleeper_teams_{season}.json"
    if not teams_cache.exists():  # schedule saved before teams were kept: download it now
        statuses = game_statuses(season, is_current=True, max_age=0)
    return statuses_by_team(statuses, _load(teams_cache), day)


def game_logs(sleeper_id: str, season: str, is_current: bool,
              max_age: float = CURRENT_SEASON_MAX_AGE) -> list[dict]:
    """One player's finished games for a season ("2026-27"), most recent first."""
    return played_games(*games_and_statuses(sleeper_id, season, is_current, max_age))


def games_and_statuses(sleeper_id: str, season: str, is_current: bool,
                       max_age: float = CURRENT_SEASON_MAX_AGE) -> tuple[dict, dict]:
    """Every game of his we have ({game_id: game}, games he sat out included),
    and the status of every game in the season ({game_id: status}).

    Past seasons are downloaded once. This season is downloaded again when the
    cache is older than `max_age` seconds, or older than the schedule: a game
    that was still going when we saved it may be complete now, and the line
    we saved for it would be partial.
    """
    statuses = game_statuses(season, is_current, max_age)
    cache = DATA_DIR / f"sleeper_{sleeper_id}_{season}.json"
    schedule_cache = DATA_DIR / f"sleeper_schedule_{season}.json"
    saved = _load(cache)
    stale = (not cache.exists()
             or is_current and (_age(cache) >= max_age
                                or cache.stat().st_mtime < schedule_cache.stat().st_mtime))
    if stale:
        url = (f"/stats/nba/player/{sleeper_id}?season_type=regular"
               f"&season={sleeper_season(season)}&grouping=week")
        saved = merge_games(saved, parse_player_games(_get_json(url)))
        cache.write_text(json.dumps(saved), encoding="utf-8")
    return saved, statuses
