"""Turn a player's game logs into the numbers the lock-in engine needs."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, stdev

from .lockin import GameForecast
from .scoring import fantasy_points

RECENT_GAMES = 25      # how many recent games to average over
MIN_GAMES = 3          # fewer than this and we don't trust the numbers
MIN_SWING = 4.0        # never assume a player is perfectly consistent
SEASON_GAMES = 82
DEFAULT_PLAY_PROB = 0.90  # for players with no previous season (rookies)


@dataclass(frozen=True)
class PlayerProfile:
    name: str
    games_used: int
    average: float     # expected fantasy points when he plays
    swing: float       # standard deviation of his fantasy points
    play_prob: float   # chance he plays in any given game

    def forecast(self) -> GameForecast:
        return GameForecast(mean=self.average, sd=self.swing, play_prob=self.play_prob)


def estimate_play_prob(last_season_games: int | None) -> float:
    """Simple v1 estimate: share of last season's games he played.

    Limitation: a player who missed time with one long injury looks less
    reliable than he is. Improving this is on the roadmap.
    """
    if not last_season_games:
        return DEFAULT_PLAY_PROB
    return min(0.98, max(0.5, last_season_games / SEASON_GAMES))


def build_profile(name: str, boxes_recent_first: list[dict], scoring: dict,
                  last_season_games: int | None) -> PlayerProfile | None:
    """Profile from his most recent games (this season first, then last season)."""
    points = [fantasy_points(b, scoring) for b in boxes_recent_first[:RECENT_GAMES]]
    if len(points) < MIN_GAMES:
        return None
    return PlayerProfile(
        name=name,
        games_used=len(points),
        average=round(mean(points), 1),
        swing=round(max(MIN_SWING, stdev(points)), 1),
        play_prob=round(estimate_play_prob(last_season_games), 2),
    )
