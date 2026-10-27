"""Lineup slots and the weekly lock-or-pass plan for Lock-In Mode.

How the league works (from Santiago):
  * Each starting slot ends the week with exactly one game.
  * After a game you can LOCK it (slot and player are done for the week)
    or pass. Benching a player clears his game, so the slot opens up for
    anyone on the roster who is eligible for it.
  * An unlocked slot at the end of the week keeps the last game played in it.
  * A player can fill any slot his Sleeper positions allow, and can be moved
    between starting slots before locking without losing his score.

Key modeling point: each player can only be locked ONCE per week, so each
open slot is really waiting on one player. For every available player we
compute his "week value" (the score we'd expect to lock from his remaining
games, via the lock-in math), then match players to open slots so the total
is as large as possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .lockin import GameForecast, value_of_waiting

# Which Sleeper positions can fill each slot type. None = anyone.
SLOT_ELIGIBILITY: dict[str, set[str] | None] = {
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"}, "UTIL": None,
}


def can_fill(slot_kind: str, positions: set[str]) -> bool:
    allowed = SLOT_ELIGIBILITY.get(slot_kind)
    return allowed is None or bool(allowed & positions)


def slot_labels(starting_positions: list[str]) -> list[str]:
    """['PG', 'UTIL', 'UTIL'] -> ['PG', 'UTIL1', 'UTIL2'] so every slot has a unique name."""
    labels = []
    for kind in starting_positions:
        if starting_positions.count(kind) > 1:
            n = sum(1 for k in starting_positions[: len(labels) + 1] if k == kind)
            labels.append(f"{kind}{n}")
        else:
            labels.append(kind)
    return labels


@dataclass
class Slot:
    label: str           # unique name, e.g. "UTIL2"
    kind: str            # slot type, e.g. "UTIL"
    player_id: str | None
    score: float         # points from the last game played in this slot
    locked: bool = False


@dataclass(frozen=True)
class FutureGame:
    player_id: str
    day: date
    forecast: GameForecast
    opponent: str = ""

    @property
    def expected(self) -> float:
        return self.forecast.mean * self.forecast.play_prob


def best_assignment(weights: list[list[float]]) -> list[int]:
    """Hungarian algorithm: pick one column per row to maximize the total.

    weights[i][j] = value of giving column j to row i (rows <= columns).
    Returns the chosen column for each row. This is the classic
    "assignment problem"; it runs in O(n^3), fast for a 9-slot lineup.
    """
    n_rows = len(weights)
    if n_rows == 0:
        return []
    n = max(n_rows, len(weights[0]))
    big = max((w for row in weights for w in row), default=0.0) + 1.0
    # Square cost matrix (minimize big - weight); padding costs `big`.
    cost = [[big - (weights[i][j] if i < n_rows and j < len(weights[i]) else 0.0)
             for j in range(n)] for i in range(n)]
    INF = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (n + 1)
    match, way = [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        match[0], j0 = i, 0
        minv, used = [INF] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = match[j0], INF, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if match[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            match[j0] = match[j1]
            j0 = j1
    result = [0] * n
    for j in range(1, n + 1):
        result[match[j] - 1] = j - 1
    return result[:n_rows]


OWN_SLOT_BONUS = 0.01  # on a tie, a player stays in the slot he's already in
DEFAULT_MIN_LOCK = 25.0  # Santiago's rule of thumb: rarely lock a non-star below ~25


def player_values(future: dict[date, list[FutureGame]]) -> dict[str, float]:
    """Each player's week value: the score we'd expect to end up locking from
    his remaining games if a slot is dedicated to him (lock-in math)."""
    by_player: dict[str, list[FutureGame]] = {}
    for day in sorted(future):
        for g in future[day]:
            by_player.setdefault(g.player_id, []).append(g)
    return {pid: value_of_waiting([g.forecast for g in games]) for pid, games in by_player.items()}


@dataclass
class WeekPlan:
    assignment: dict[str, str | None]   # open slot label -> planned player
    values: dict[str, float]             # player -> week value
    total: float

    def slot_value(self, label: str) -> float:
        pid = self.assignment.get(label)
        return self.values.get(pid, 0.0) if pid else 0.0


def plan_week(slots: list[Slot], values: dict[str, float],
              positions: dict[str, set[str]]) -> WeekPlan:
    """Match available players to open slots (each player at most one slot)
    to maximize total expected points. Solved exactly with best_assignment."""
    open_slots = [s for s in slots if not s.locked]
    pids = [p for p, v in values.items() if v > 0]
    assignment: dict[str, str | None] = {s.label: None for s in open_slots}
    if not open_slots or not pids:
        return WeekPlan(assignment, values, 0.0)
    weights = [[(values[p] + (OWN_SLOT_BONUS if p == s.player_id else 0.0))
                if can_fill(s.kind, positions.get(p, set())) else 0.0
                for p in pids] for s in open_slots]
    total = 0.0
    for slot, j in zip(open_slots, best_assignment(weights)):
        if j < len(pids) and can_fill(slot.kind, positions.get(pids[j], set())):
            assignment[slot.label] = pids[j]
            total += values[pids[j]]
    return WeekPlan(assignment, values, total)


def alternatives(slot: Slot, plan: WeekPlan, positions: dict[str, set[str]]) -> list[str]:
    """Other available players who could fill this slot, best first."""
    return sorted((p for p, v in plan.values.items()
                   if v > 0 and p != plan.assignment.get(slot.label)
                   and can_fill(slot.kind, positions.get(p, set()))),
                  key=lambda p: plan.values[p], reverse=True)


def depth_label(slot: Slot, plan: WeekPlan, positions: dict[str, set[str]]) -> str:
    n = len(alternatives(slot, plan, positions))
    return "THIN" if n <= 1 else "DEEP" if n >= 4 else "OK"


@dataclass(frozen=True)
class LockMove:
    player_id: str
    from_label: str
    to_label: str
    score: float
    gain: float       # expected points this lock adds to the week vs passing


def pass_values(slots: list[Slot], values: dict[str, float]) -> dict[str, float]:
    """Week values if every score in hand is passed on.

    A player with games left gets his next game instead (it replaces his
    score), so his value is his week value. A player with no games left keeps
    his score as long as he stays in a slot, so his value is that score.
    """
    vals = dict(values)
    for s in slots:
        if not s.locked and s.player_id and s.score > 0 and s.player_id not in values:
            vals[s.player_id] = s.score
    return vals


def recommend_locks(slots: list[Slot], values: dict[str, float],
                    positions: dict[str, set[str]], min_lock: float = DEFAULT_MIN_LOCK
                    ) -> tuple[list[LockMove], list[LockMove]]:
    """Which scores in hand to lock, and in which slot.

    For each player with a finished game in an unlocked slot, try locking him
    into every open slot he's eligible for, and compare the whole lineup's
    expected points (his score + the best matching of everyone else to the
    remaining slots) against passing. Take the best lock and repeat.

    Scores under `min_lock` are only locked when the slot would otherwise have
    nobody left to fill it. Returns (locks, held_back): held_back are locks the
    math liked but the minimum blocked, so the user can override if they want.
    """
    work = [Slot(s.label, s.kind, s.player_id, s.score, s.locked) for s in slots]
    vals = pass_values(work, values)
    in_hand = [(s.player_id, s.label, s.score) for s in work
               if not s.locked and s.player_id and s.score > 0]
    moves: list[LockMove] = []
    held: dict[str, LockMove] = {}
    while in_hand:
        base = plan_week(work, vals, positions)
        best = None
        for pid, from_label, score in in_hand:
            rest = {p: v for p, v in vals.items() if p != pid}
            for k in work:
                if k.locked or not can_fill(k.kind, positions.get(pid, set())):
                    continue
                k.locked = True
                gain = score + plan_week(work, rest, positions).total - base.total
                k.locked = False
                move = LockMove(pid, from_label, k.label, score, round(gain, 1))
                # A player with no games left keeps his score anyway, so locking
                # him in his best slot is free insurance against later mistakes.
                free = pid not in values and gain > -0.05
                if gain <= 0.05 and not free:
                    continue
                nobody_else = not any(p != pid and v > 0 and can_fill(k.kind, positions.get(p, set()))
                                      for p, v in vals.items())
                if score < min_lock and not nobody_else:
                    if pid not in held or gain > held[pid].gain:
                        held[pid] = move
                    continue
                if best is None or gain > best.gain:
                    best = move
        if best is None:
            break
        moves.append(best)
        for k in work:
            if k.label == best.to_label:
                k.locked, k.player_id, k.score = True, best.player_id, best.score
        vals.pop(best.player_id, None)
        held.pop(best.player_id, None)
        in_hand = [h for h in in_hand if h[0] != best.player_id]
    return moves, list(held.values())


def apply_moves(slots: list[Slot], moves: list[LockMove], positions: dict[str, set[str]],
                name=str) -> tuple[list[Slot], list[str]]:
    """Carry out the recommended locks on a copy of the lineup.

    Returns the lineup afterwards and step-by-step Sleeper instructions.
    Sleeper rule: a straight swap only works if the other player is eligible
    for the slot being vacated; benching a player clears his last game.
    """
    after = [Slot(s.label, s.kind, s.player_id, s.score, s.locked) for s in slots]
    by_label = {s.label: s for s in after}
    being_locked = {m.player_id for m in moves}
    steps = []
    for m in moves:
        p = name(m.player_id)
        src, dst = by_label[m.from_label], by_label[m.to_label]
        if src is dst:
            steps.append(f"Lock {p} in {m.to_label} ({m.score:.1f}).")
        elif dst.player_id is None:
            steps.append(f"Move {p} from {m.from_label} to the empty {m.to_label}, "
                         f"then lock ({m.score:.1f}).")
            src.player_id, src.score = None, 0.0
        elif can_fill(src.kind, positions.get(dst.player_id, set())):
            steps.append(f"Swap {p} ({m.from_label}) with {name(dst.player_id)} ({m.to_label}), "
                         f"then lock {p} in {m.to_label} ({m.score:.1f}).")
            src.player_id, src.score = dst.player_id, dst.score
        else:
            q = dst.player_id
            warn = (f" This clears his {dst.score:.1f} from his last game."
                    if dst.score > 0 and q not in being_locked else "")
            steps.append(f"{name(q)} can't play {m.from_label}, so bench him (or move him to another "
                         f"slot he's eligible for) first.{warn} Then move {p} from {m.from_label} "
                         f"to {m.to_label} and lock ({m.score:.1f}).")
            src.player_id, src.score = None, 0.0
        dst.player_id, dst.score, dst.locked = m.player_id, m.score, True
    return after, steps


def move_instructions(slots: list[Slot], moves: list[LockMove],
                      positions: dict[str, set[str]], name=str) -> list[str]:
    return apply_moves(slots, moves, positions, name)[1]
