"""Game logs from NBA.com via the nba_api package.

NBA.com blocks requests from cloud servers, so this runs on your own
computer. Results are cached in data/gamelogs/ so each player is only
downloaded once (past seasons) or a few times a day (current season).
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "gamelogs"
CURRENT_SEASON_MAX_AGE = 6 * 60 * 60  # refresh this season's logs every 6 hours
REQUEST_PAUSE = 0.6  # be polite to NBA.com between downloads
RETRY_WAITS = [3, 8, 20]  # seconds to wait before each retry if NBA.com hangs up
TIMEOUT = 60

# If a Sleeper name doesn't match NBA.com, map it by hand here:
# "sleeper name": NBA.com player id
NAME_OVERRIDES: dict[str, int] = {}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """'Jabari Smith Jr.' -> 'jabari smith', 'Nikola Jokić' -> 'nikola jokic'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z ]", "", ascii_name.lower().replace("-", " ")).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def find_nba_id(name: str) -> int | None:
    """NBA.com id for a player name, preferring active players."""
    if name in NAME_OVERRIDES:
        return NAME_OVERRIDES[name]
    from nba_api.stats.static import players  # bundled list, no download

    target = normalize_name(name)
    matches = [p for p in players.get_players() if normalize_name(p["full_name"]) == target]
    active = [p for p in matches if p["is_active"]]
    pick = active or matches
    return pick[0]["id"] if len(pick) == 1 else None


def _to_box(row: dict) -> dict:
    """NBA.com game-log row -> the lowercase box-score dict scoring.py expects."""
    keys = ["PTS", "REB", "OREB", "AST", "STL", "BLK", "TOV", "PF",
            "FGM", "FGA", "FG3M", "FTM", "FTA", "MIN"]
    box = {k.lower(): row.get(k) or 0 for k in keys}
    box["game_date"] = row.get("GAME_DATE")
    box["matchup"] = row.get("MATCHUP")
    return box


class NBAStatsUnavailable(Exception):
    """NBA.com kept refusing the connection after several tries."""


def _download_with_retries(nba_id: int, season: str) -> dict:
    from nba_api.stats.endpoints import playergamelog

    last_error = None
    for wait in [REQUEST_PAUSE] + RETRY_WAITS:
        time.sleep(wait)
        try:
            return playergamelog.PlayerGameLog(
                player_id=nba_id, season=season, timeout=TIMEOUT
            ).get_normalized_dict()
        except Exception as err:  # NBA.com errors come in many shapes
            last_error = err
    raise NBAStatsUnavailable(str(last_error))


def game_logs(nba_id: int, season: str, is_current: bool) -> list[dict]:
    """One player's games for a season ("2026-27"), most recent first."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = DATA_DIR / f"{nba_id}_{season}.json"
    if cache.exists():
        fresh = time.time() - cache.stat().st_mtime < CURRENT_SEASON_MAX_AGE
        if fresh or not is_current:
            return json.loads(cache.read_text(encoding="utf-8"))

    rows = _download_with_retries(nba_id, season)
    boxes = [_to_box(r) for r in rows["PlayerGameLog"]]
    cache.write_text(json.dumps(boxes), encoding="utf-8")
    return boxes
