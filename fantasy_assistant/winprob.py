"""Win probability: simulate the rest of the week many times and count how
often your total beats your opponent's.

One simulated week:

  1. Every remaining game of every player on both teams is drawn up front,
     in a fixed order (player id, then day): does he play (his chance to
     play), and if so, what does he score (a bell curve around his average
     with his swing, the same unclipped normal the lock-in math in lockin.py
     uses). Each simulated week k has its own random generator, seeded from
     (seed, k), so the draws never depend on what's being compared: skipping
     a lock can't shift anyone else's games.
  2. Each team's slots then play out under a lock policy (SlotPlan):
       * locked slots keep their score;
       * "app" slots wait on one planned player (the week plan from
         lineup.plan_week) and lock his first game that beats his lock bar
         (lockin.value_of_waiting of his games after it) and that day's floor;
         otherwise his next game replaces it, and his last game stands;
       * "passive" slots never lock: they end with the player's last game;
       * a game he sits out scores 0 (rule 3).
  3. Your total vs. theirs: a win counts 1, a tie 1/2.

Why a fixed plan instead of rerunning the whole-lineup engine (lineup.py)
every simulated day: one run of recommend_locks takes about 0.1 to 0.3
seconds, so thousands of weeks times every day left would take hours. The
fixed plan is the same one-player-per-slot approximation behind advise's Exp
column, which gives a check: with the floor off, the average simulated total
matches the plan's expected total (a test does this).

Left out, for now: free rolls and replanning during the week, the partial
score of a game that's on right now (it's drawn from scratch), and links
between players (teammates, blowouts).

Nothing here downloads, prints or saves; service.win_probability builds the
inputs from the lineups and advice.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist
from typing import Callable, Iterator

from .lineup import FutureGame
from .lockin import value_of_waiting

SIMULATIONS = 10_000   # about +-1 percentage point (95% confidence) on the win probability
SEED = 2026            # fixed, so the same inputs always give the same number

APP, PASSIVE = "app", "passive"

_standard_normal = NormalDist()


@dataclass(frozen=True)
class SlotPlan:
    """How one starting slot ends the week in the simulation.

    `player_id` is the player whose games decide it (None: nobody, so it
    keeps `score`). If he has no games left, or the slot is locked, it keeps
    `score` too.
    """
    label: str
    player_id: str | None
    score: float
    locked: bool = False
    policy: str = APP      # APP: locks by his bars and the floor; PASSIVE: keeps his last game


@dataclass(frozen=True)
class TeamWeek:
    """One team's slots for the rest of the week."""
    slots: tuple[SlotPlan, ...]

    def settled(self, games: dict[str, list[FutureGame]]) -> bool:
        """True if no slot has a game left to decide it: the totals are final."""
        return all(s.locked or s.player_id not in games for s in self.slots)

    def total_now(self) -> float:
        """The total if nothing else happens (each slot's current or kept score)."""
        return sum(s.score for s in self.slots)


@dataclass(frozen=True)
class Odds:
    """One scenario's result over all the simulated weeks."""
    win: float          # chance my total beats theirs (a tie counts half), 0 to 1
    mine: float         # my average simulated total
    theirs: float       # their average simulated total

    @property
    def percent(self) -> int:
        """The win probability as a whole percent, as advise prints it."""
        return round(100 * self.win)


def lock_bars(games: list[FutureGame], floor: Callable[[date], float] | None) -> list[float | None]:
    """For each of a player's games, the lowest score worth locking right after it.

    The bar is what waiting on his later games is worth (lockin.value_of_waiting),
    raised to that day's floor when `floor` is given. His last game has no bar
    (None): there's nothing left to wait for, so its score stands.
    """
    bars: list[float | None] = []
    for i, game in enumerate(games):
        if i == len(games) - 1:
            bars.append(None)
            continue
        bar = value_of_waiting([g.forecast for g in games[i + 1:]])
        if floor is not None:
            bar = max(bar, floor(game.day))
        bars.append(bar)
    return bars


def week_seed(seed: int, k: int) -> str:
    """Simulated week k's seed. random.seed() takes an int or a string (not a
    tuple, on Python 3.11+), and a string like '2026:17' is turned into the
    same generator on every Python version and computer."""
    return f"{seed}:{k}"


def draw_week(order: list[tuple[str, list[FutureGame]]], seed: int, k: int
              ) -> dict[str, list[float | None]]:
    """Simulated week k: every game's score (None if he sat it out).

    Every game always takes exactly two random numbers (plays?, score), in
    the fixed `order`, so each game's draw depends only on (seed, k) and its
    place in the order, never on what the policies do with it.

    The score uses inverse_cdf on a uniform number from random(), which
    Python guarantees is the same on every version; random.gauss isn't.
    """
    rng = random.Random(week_seed(seed, k))
    drawn = {}
    for pid, games in order:
        scores: list[float | None] = []
        for g in games:
            plays = rng.random() < g.forecast.play_prob
            u = min(max(rng.random(), 1e-12), 1 - 1e-12)   # inv_cdf needs 0 < u < 1
            fc = g.forecast
            score = fc.mean + fc.sd * _standard_normal.inv_cdf(u) if fc.sd > 0 else fc.mean
            scores.append(score if plays else None)
        drawn[pid] = scores
    return drawn


def slot_score(slot: SlotPlan, drawn: dict[str, list[float | None]],
               bars: dict[str, list[float | None]]) -> float:
    """How one slot ends this simulated week."""
    pid = slot.player_id
    if slot.locked or pid not in drawn:
        return slot.score
    games = drawn[pid]
    if slot.policy == PASSIVE:
        last = games[-1]
        return last if last is not None else 0.0   # sat out his last game: 0 (rule 3)
    for score, bar in zip(games, bars[pid]):
        if bar is None:                    # his last game: it stands
            return score if score is not None else 0.0
        if score is not None and score >= bar:
            return score                   # locked
    return 0.0  # not reached: the last game always has bar None


def game_order(games: dict[str, list[FutureGame]]) -> list[tuple[str, list[FutureGame]]]:
    """The fixed draw order: by player id, each player's games by day."""
    return [(pid, sorted(games[pid], key=lambda g: g.day)) for pid in sorted(games)]


def simulated_weeks(teams: list[TeamWeek], games: dict[str, list[FutureGame]],
                    floor: Callable[[date], float] | None,
                    sims: int = SIMULATIONS, seed: int = SEED) -> Iterator[list[dict[str, float]]]:
    """Each simulated week's slot scores, {slot: score} per team in `teams`.

    All the teams share the week's draws, so comparing two versions of your
    lineup (with and without a lock) shows the lock's effect, not luck.
    `floor` (e.g. lineup.lock_floor) holds back locks under it, day by day;
    None turns it off.
    """
    order = game_order(games)
    bars = {pid: lock_bars(gs, floor) for pid, gs in order}
    for k in range(sims):
        drawn = draw_week(order, seed, k)
        yield [{s.label: slot_score(s, drawn, bars) for s in team.slots} for team in teams]


def simulate(scenarios: list[TeamWeek], theirs: TeamWeek, games: dict[str, list[FutureGame]],
             floor: Callable[[date], float] | None,
             sims: int = SIMULATIONS, seed: int = SEED) -> list[Odds]:
    """The win probability of each of your `scenarios` against `theirs`.

    `games`: every player's remaining games, both teams (each player's games
    in day order). Returns one Odds per scenario, in order.
    """
    n = len(scenarios)
    wins, mine_sum, theirs_sum = [0.0] * n, [0.0] * n, 0.0
    for week in simulated_weeks(scenarios + [theirs], games, floor, sims, seed):
        their_total = sum(week[-1].values())
        theirs_sum += their_total
        for i in range(n):
            my_total = sum(week[i].values())
            mine_sum[i] += my_total
            wins[i] += 1.0 if my_total > their_total else 0.5 if my_total == their_total else 0.0
    return [Odds(wins[i] / sims, mine_sum[i] / sims, theirs_sum / sims) for i in range(n)]
