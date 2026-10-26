"""Lock-In Mode decision engine.

In Lock-In Mode only one game per player counts each week. After a player
finishes a game you either LOCK that score or PASS and hope a later game is
better. Once you pass, that game is gone for good.

This is an "optimal stopping" problem, solved by working backward from the
player's last game of the week:

  * Last game: you take whatever he scores (or 0 if he sits, or if the
    score is negative you would rather pass). Its value is E[max(X, 0)].
  * Any earlier game: lock only if the score beats the value of waiting.
    The value of waiting is what you expect to end up with if you keep
    playing this same rule on the games that are left.

Each game's score X is modeled as a normal distribution (mean, sd), and
each game has a probability the player actually plays.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist


@dataclass(frozen=True)
class GameForecast:
    """What we expect from one upcoming game."""

    mean: float          # expected fantasy points if he plays
    sd: float            # how much his score usually swings
    play_prob: float = 1.0  # chance he plays at all (rest, injury)


def expected_max(mean: float, sd: float, floor: float) -> float:
    """E[max(X, floor)] for X ~ Normal(mean, sd).

    "If he scores below `floor`, I keep `floor` instead." That is exactly
    what happens when you can fall back on the value of waiting.
    """
    if sd <= 0:
        return max(mean, floor)
    z = (floor - mean) / sd
    std = NormalDist()
    return floor * std.cdf(z) + mean * (1 - std.cdf(z)) + sd * std.pdf(z)


def value_of_waiting(remaining: list[GameForecast]) -> float:
    """Expected points you end up with if you PASS now and play optimally
    on the `remaining` games (in the order they happen)."""
    value = 0.0  # no games left -> you get nothing
    for game in reversed(remaining):
        played = expected_max(game.mean, game.sd, value)
        value = game.play_prob * played + (1 - game.play_prob) * value
    return value


@dataclass(frozen=True)
class LockDecision:
    score: float
    threshold: float
    lock: bool

    @property
    def margin(self) -> float:
        """How far the score is above (+) or below (-) the threshold."""
        return self.score - self.threshold


def decide(score: float, remaining: list[GameForecast]) -> LockDecision:
    """Should you lock a game where the player just scored `score`?"""
    threshold = value_of_waiting(remaining)
    return LockDecision(score=score, threshold=threshold, lock=score >= threshold)
