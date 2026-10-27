"""Small client for Sleeper's free, read-only API (no key needed).

Docs: https://docs.sleeper.com/
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE_URL = "https://api.sleeper.app/v1"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLAYERS_CACHE_MAX_AGE = 24 * 60 * 60  # Sleeper asks for at most one pull per day


def _get(path: str):
    resp = requests.get(f"{BASE_URL}{path}", timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_user(username: str) -> dict:
    return _get(f"/user/{username}")


def get_league(league_id: str) -> dict:
    return _get(f"/league/{league_id}")


def get_rosters(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/rosters")


def get_league_users(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/users")


def get_state(sport: str = "nba") -> dict:
    """Current season and week number."""
    return _get(f"/state/{sport}")


def get_matchups(league_id: str, week: int) -> list[dict]:
    """Every team's starters and per-slot points for a week."""
    return _get(f"/league/{league_id}/matchups/{week}")


def get_players(sport: str = "nba") -> dict:
    """Every player Sleeper knows about, keyed by Sleeper player id.

    The file is several MB, so it's cached in data/ and refreshed daily.
    """
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / f"players_{sport}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < PLAYERS_CACHE_MAX_AGE:
        return json.loads(cache.read_text(encoding="utf-8"))
    players = _get(f"/players/{sport}")
    cache.write_text(json.dumps(players), encoding="utf-8")
    return players


def find_my_roster(rosters: list[dict], user_id: str) -> dict | None:
    for roster in rosters:
        if roster.get("owner_id") == user_id:
            return roster
    return None
