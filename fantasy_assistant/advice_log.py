"""Advice log: remember what `advise` said, then check it against what happened.

Every `advise` run is saved to advice_log.json (in data/, or the folder
STATE_DIR in .env names; see state.py) as one entry: each slot's
status, the score in hand, who the slot was waiting on and his expected value,
the recommended and held-back locks, and the time. `review` grades those calls
once games have been played:

  LOCK  Graded on the player's NEXT game, because passing would have meant that
        game replaced the score. Right if it was lower (or he sat out).
  PASS  Graded on the same next game, the other way round: right if it beat the
        score that was passed on, wrong if it was lower or he sat out. How the
        slot finished is shown too, as information only.
  HELD  A lock the math liked but the lock floor blocked. Graded like a
        PASS, so we can see whether the floor is paying off.

Why the next game and not the slot (decided 11/12): the slot's final score
mixes in everything that happened after the call (other players moved in,
later locks, Santiago's own choices), so it doesn't say whether this one call
was right. The next game is exactly what passing bet on, and grading LOCK and
PASS on the same game makes them mirror images: for any next game, exactly one
of the two calls is right.

Any call for a player with no games left is FREE, not graded: he keeps his
score by staying in the slot, so locking and passing were worth the same.

If Santiago locked a score anyway after the app said PASS (or HELD), the app's
call is still graded on the next game, and marked as his override. Since he did
the opposite, his override beat the app whenever the app's call is WRONG.

If several runs repeat the same call (same player, same score, same week), it is
graded once, under the first run that made it. The grading here is pure (no
network), so it's easy to test; __main__.py fetches the data it needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

# state.use() moves this to the folder STATE_DIR names, when it's set.
LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "advice_log.json"

# Verdicts
RIGHT, WRONG, PENDING = "RIGHT", "WRONG", "PENDING"
FREE = "FREE"        # a lock with no games left: nothing to lose, so not graded

SAME_SCORE = 0.05  # scores closer than this are the same game


# ---------- Saving and notes ----------

# Every function takes `path=None` and looks LOG_PATH up when it runs. A default
# of `path=LOG_PATH` would be fixed when this file is imported, so pointing
# LOG_PATH somewhere else (tests, scratch runs) would still write the real log.

def load(path: Path | None = None) -> list[dict]:
    path = path or LOG_PATH
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_all(entries: list[dict], path: Path | None = None) -> None:
    path = path or LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def append(entry: dict, path: Path | None = None) -> int:
    """Save one advise run. Gives it the next id number and returns it."""
    path = path or LOG_PATH
    entries = load(path)
    entry["id"] = max((e["id"] for e in entries), default=0) + 1
    entry.setdefault("notes", [])
    entries.append(entry)
    save_all(entries, path)
    return entry["id"]


def merge(mine: list[dict], theirs: list[dict]) -> list[dict]:
    """Combine two computers' advice logs into one, keeping every run and note.

    Runs are put in time order and renumbered 1, 2, 3, ... (review grades a
    repeated call under the lowest-numbered run, so the numbers must follow
    the clock). A run that's in both logs, e.g. from a copied file, is kept
    once with the notes from both. Each run's notes are in time order too.
    """
    def same_run(entry):  # everything but the number and the notes
        return json.dumps({k: v for k, v in entry.items() if k not in ("id", "notes")},
                          sort_keys=True)

    runs: dict[str, dict] = {}
    for entry in mine + theirs:
        key = same_run(entry)
        if key not in runs:
            runs[key] = {**entry, "notes": []}
        for note in entry.get("notes", []):
            if note not in runs[key]["notes"]:
                runs[key]["notes"].append(note)

    merged = sorted(runs.values(), key=lambda e: e["time"])  # ISO times sort as text
    for number, entry in enumerate(merged, start=1):
        entry["id"] = number
        entry["notes"].sort(key=lambda n: n["time"])
    return merged


def add_note(entry_id: int, text: str, slot: str | None = None,
             path: Path | None = None) -> dict:
    """Attach a note (e.g. why you disagreed) to a saved run. Raises KeyError
    if there's no run with that id."""
    path = path or LOG_PATH
    entries = load(path)
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if entry is None:
        raise KeyError(entry_id)
    entry.setdefault("notes", []).append(
        {"time": datetime.now().isoformat(timespec="seconds"), "slot": slot, "text": text})
    save_all(entries, path)
    return entry


def one_line(entry: dict) -> str:
    """Short summary of a run's calls, for the `note` listing."""
    parts = []
    for c in calls_in(entry):
        where = f"in {c.lock_slot}" if c.kind == "LOCK" else f"({c.slot})"
        parts.append(f"{c.kind} {c.player} {c.score:.1f} {where}")
    return "; ".join(parts) or "no scores in hand"


# ---------- What happened ----------

@dataclass(frozen=True)
class SlotOutcome:
    """Where a slot stands now, from Sleeper plus the saved locks."""
    player_id: str | None
    score: float
    locked: bool
    final: bool  # locked, or the week is over


@dataclass(frozen=True)
class GameResult:
    """One of a player's games after the advice was given."""
    day: date
    points: float | None  # None = hasn't been played yet
    dnp: bool = False     # the team played but he didn't

    def text(self) -> str:
        label = self.day.strftime("%a")
        if self.dnp:
            return f"{label} DNP"
        return f"{label} {self.points:.1f}" if self.points is not None else f"{label} to come"


def week_is_over(week_start: str, today: date) -> bool:
    return today > date.fromisoformat(week_start) + timedelta(days=6)


def later_games(entry: dict, player_id: str, points_by_day: dict[date, float],
                today: date) -> list[GameResult]:
    """The player's games that were still to come when the advice was given.

    A game on a past day with no box score means he didn't play. A game today
    with no box score yet is treated as not played yet.
    """
    results = []
    for day_text in entry.get("games_left", {}).get(player_id, []):
        day = date.fromisoformat(day_text)
        if day in points_by_day:
            results.append(GameResult(day, points_by_day[day]))
        elif day < today:
            results.append(GameResult(day, 0.0, dnp=True))
        else:
            results.append(GameResult(day, None))
    return results


# ---------- Grading ----------

@dataclass
class Call:
    """One piece of advice: lock, pass, or a lock held back by the floor."""
    run_id: int
    week_start: str
    kind: str              # LOCK, PASS, or HELD
    slot: str              # where the player sat (PASS/HELD) or should be locked (LOCK)
    lock_slot: str | None  # LOCK/HELD: the slot the lock was for
    player_id: str
    player: str
    score: float
    also_in: list[int] = field(default_factory=list)  # later runs that repeated it
    later: list[GameResult] = field(default_factory=list)
    verdict: str = PENDING
    detail: str = ""
    override: bool = False  # PASS/HELD: you locked this score anyway

    def key(self) -> tuple:
        return (self.week_start, self.kind, self.player_id, round(self.score, 1))


def calls_in(entry: dict) -> list[Call]:
    """Every LOCK, HELD, and PASS call made in one saved run."""
    names = entry.get("names", {})
    run, week = entry["id"], entry["week_start"]
    # Where each player sits after the recommended moves (swaps can move people).
    seat = {r["player_id"]: r["slot"] for r in entry["slots"] if r.get("player_id")}
    held_ids = {h["player_id"] for h in entry["held"]}
    calls = []
    for m in entry["locks"]:
        calls.append(Call(run, week, "LOCK", m["to"], m["to"], m["player_id"],
                          names.get(m["player_id"], "?"), m["score"]))
    for h in entry["held"]:
        calls.append(Call(run, week, "HELD", seat.get(h["player_id"], h["from"]), h["to"],
                          h["player_id"], names.get(h["player_id"], "?"), h["score"]))
    for r in entry["slots"]:
        if r["status"] == "PASS" and r["player_id"] not in held_ids:
            calls.append(Call(run, week, "PASS", r["slot"], None, r["player_id"],
                              names.get(r["player_id"], "?"), r["score"]))
    return calls


def _locked_this_score(call: Call, outcomes: dict[str, SlotOutcome]) -> bool:
    """Did this exact score end up locked in some slot?"""
    return any(o.locked and o.player_id == call.player_id
               and abs(o.score - call.score) < SAME_SCORE for o in outcomes.values())


def grade_lock(call: Call) -> None:
    """LOCK: right if his next game would not have beaten the score."""
    if not call.later:
        call.verdict, call.detail = FREE, "no games left, so locking cost nothing"
        return
    nxt = call.later[0]
    if nxt.points is None:
        call.verdict, call.detail = PENDING, f"next game {nxt.day:%a}"
    elif nxt.points <= call.score:
        how = "he sat" if nxt.dnp else f"{nxt.points:.1f}"
        call.verdict, call.detail = RIGHT, f"next game {how}, lower"
    else:
        call.verdict, call.detail = WRONG, f"next game {nxt.points:.1f} would have beaten it"


def slot_text(call: Call, outcomes: dict[str, SlotOutcome], names: dict[str, str]) -> str:
    """How the slot the call was about finished (or stands so far). Information
    only: it isn't used for the grade."""
    o = outcomes.get(call.slot)
    if o is None:
        return ""
    who = names.get(o.player_id, "?") if o.player_id else "nobody"
    return f"slot {'ended at' if o.final else 'so far'} {o.score:.1f} ({who})"


def grade_pass(call: Call, outcomes: dict[str, SlotOutcome], names: dict[str, str]) -> None:
    """PASS / HELD: right if his next game beat the score passed on.

    The mirror image of grade_lock: a tie or a DNP means passing gained
    nothing, so it's wrong here and right for a LOCK.
    """
    slot = slot_text(call, outcomes, names)
    if not call.later:
        # No games left: he keeps the score by staying in the slot, so locking
        # and passing were worth the same. Nothing to grade (same as LOCK).
        call.verdict, call.detail = FREE, "no games left, so passing cost nothing"
        return
    nxt = call.later[0]
    # Locking it anyway doesn't stop us grading the app's call: his next game
    # is still what passing would have bet on.
    call.override = _locked_this_score(call, outcomes)
    if nxt.points is None:
        call.verdict, call.detail = PENDING, f"next game {nxt.day:%a}"
    elif nxt.points > call.score:
        call.verdict, call.detail = RIGHT, f"next game {nxt.points:.1f} beat it"
    else:
        how = "he sat" if nxt.dnp else f"{nxt.points:.1f}, " + (
            "lower" if nxt.points < call.score else "no better")
        call.verdict, call.detail = WRONG, f"next game {how}"
    if call.override:
        why = "you overrode the floor and locked it" if call.kind == "HELD" else "you locked it anyway"
        call.detail += f"; {why}"
    elif slot:  # an override's slot is just the score you locked, already shown
        call.detail += f"; {slot}"


def review(entries: list[dict], outcomes_by_week: dict[str, dict[str, SlotOutcome]],
           points_by_player: dict[str, dict[date, float]], today: date) -> list[Call]:
    """Grade every distinct call in these runs (oldest first)."""
    first: dict[tuple, Call] = {}
    names: dict[str, str] = {}
    for entry in sorted(entries, key=lambda e: e["id"]):
        names.update(entry.get("names", {}))
        for call in calls_in(entry):
            if call.key() in first:
                first[call.key()].also_in.append(entry["id"])
                continue
            first[call.key()] = call
            call.later = later_games(entry, call.player_id,
                                     points_by_player.get(call.player_id, {}), today)
    for call in first.values():
        if call.kind == "LOCK":
            grade_lock(call)
        else:
            grade_pass(call, outcomes_by_week.get(call.week_start, {}), names)
    return list(first.values())


def forecast_check(entries: list[dict],
                   outcomes_by_week: dict[str, dict[str, SlotOutcome]]) -> list[tuple[float, float]]:
    """(expected, actual) for each open slot that has finished, using the first
    run's forecast for that slot. Shows whether the plan's Exp runs high or low."""
    pairs, seen = [], set()
    for entry in sorted(entries, key=lambda e: e["id"]):
        outcomes = outcomes_by_week.get(entry["week_start"], {})
        for r in entry["slots"]:
            key = (entry["week_start"], r["slot"])
            if r["status"] in ("LOCK", "LOCKED") or not r.get("expected") or key in seen:
                continue
            seen.add(key)
            o = outcomes.get(r["slot"])
            if o and o.final:
                pairs.append((r["expected"], o.score))
    return pairs


def override_record(calls: list[Call]) -> tuple[int, int]:
    """(beat the app, graded) for the calls you overrode. You did the opposite
    of the app, so your override won whenever the app's call was WRONG."""
    graded = [c for c in calls if c.override and c.verdict in (RIGHT, WRONG)]
    return sum(c.verdict == WRONG for c in graded), len(graded)


def tally(calls: list[Call]) -> dict[str, dict[str, int]]:
    """Count verdicts per kind: {"LOCK": {"RIGHT": 2, "WRONG": 1, ...}, ...}."""
    counts: dict[str, dict[str, int]] = {}
    for c in calls:
        kind = counts.setdefault(c.kind, {})
        kind[c.verdict] = kind.get(c.verdict, 0) + 1
    return counts
