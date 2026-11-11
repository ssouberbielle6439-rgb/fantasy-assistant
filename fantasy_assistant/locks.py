"""Remember which lineup slots are locked this week, and at what score.

Sleeper's API shows each slot's score but not whether it's locked, so the
app asks once and saves the answer for the rest of the week (in data/, or the
folder STATE_DIR in .env names; see state.py).

The score is saved too, because Sleeper's matchup numbers can't be trusted
for locked slots: on Sunday night 11/01 the API changed every minute, with
Mobley's locked F slot showing 25.3 or 27.9 and Murray's locked PG slot
showing each of his three games in turn. Once the app knows a lock, its own
record wins.

Since 11/05 the locked game's id is saved as well, and the game is re-scored
every run (slot_scores.rescore_locks), so Sleeper's stat corrections come
through. A typed score (F=27.9) is saved without a game, so it's never
re-scored.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# state.use() moves this to the folder STATE_DIR names, when it's set.
STATE_DIR = Path(__file__).resolve().parent.parent / "data"


def _path(week_start: date) -> Path:
    return STATE_DIR / f"locks_{week_start.isoformat()}.json"


def load(week_start: date) -> dict[str, dict]:
    """Saved locks for the week: slot label -> {"player_id", "score", "game_id"}.

    Files saved before scores were kept hold just the player id; those come
    back with score None (meaning: use the slot's current score). Files saved
    before 11/05 have no game_id; those come back with None (keep the score).
    """
    path = _path(week_start)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {label: {"game_id": None, **lock} if isinstance(lock, dict)
            else {"player_id": lock, "score": None, "game_id": None}
            for label, lock in raw.items()}


def save(week_start: date, locks: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _path(week_start).write_text(json.dumps(locks, indent=2), encoding="utf-8")


def merge(mine: dict, theirs: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """Combine two computers' lock files for the same week, as saved on disk.

    Every slot locked on either computer stays locked. If both locked the same
    slot differently (another player or score), there's no way to tell which is
    right, so `mine` is kept and the clash is returned as (slot, kept, other)
    for the user to check against Sleeper's app.
    """
    merged, conflicts = dict(mine), []
    for slot, lock in theirs.items():
        if slot not in merged:
            merged[slot] = lock
        elif merged[slot] != lock:
            conflicts.append((slot, json.dumps(merged[slot]), json.dumps(lock)))
    return merged, conflicts


def from_slots(slots) -> dict[str, dict]:
    """The locked slots of a lineup, in the shape save() expects."""
    return {s.label: {"player_id": s.player_id, "score": s.score, "game_id": s.game_id}
            for s in slots if s.locked and s.player_id}


def clean(answer: str) -> str:
    # Windows PowerShell 5.1 starts piped text with an invisible byte-order mark
    # (U+FEFF). str.strip() doesn't treat it as whitespace, so drop it first or
    # '' and 'PG PF' both look like unknown slot names.
    return answer.replace("﻿", "").strip()


def _split(token: str) -> tuple[str, float | None] | None:
    """'F=27.9' -> ('F', 27.9), 'PG' -> ('PG', None), 'F=abc' -> None."""
    name, has_score, score = token.partition("=")
    if not has_score:
        return name, None
    try:
        return name, float(score)
    except ValueError:
        return None


def parse_answer(answer: str, labels: list[str], saved: list[str]) -> list[str] | None:
    """Turn the user's reply into a list of locked slot labels.

    ''      -> keep the saved locks
    'none'  -> nothing locked
    'pg f util2' -> those slots (case doesn't matter)
    'pg f=27.9'  -> PG and F, with F's locked score typed in (see typed_scores)
    Returns None if the reply has a slot name that doesn't exist.
    """
    text = clean(answer)
    if not text:
        return saved
    if text.lower() == "none":
        return []
    tokens = text.replace(",", " ").split()
    if not tokens:
        return None  # just commas: only 'none' means nothing is locked
    by_lower = {label.lower(): label for label in labels}
    chosen = []
    for token in tokens:
        parts = _split(token)
        label = by_lower.get(parts[0].lower()) if parts else None
        if label is None:
            return None
        if label not in chosen:
            chosen.append(label)
    return chosen


def unlocked_by(chosen: list[str], saved: list[str]) -> list[str]:
    """Saved locks that an answer would remove (locks are permanent, so usually a typo)."""
    return [label for label in saved if label not in chosen]


def typed_scores(answer: str, labels: list[str]) -> dict[str, float]:
    """Scores typed after '=': 'pg f=27.9' -> {'F': 27.9}. For a reply parse_answer accepted."""
    by_lower = {label.lower(): label for label in labels}
    scores = {}
    for token in clean(answer).replace(",", " ").split():
        parts = _split(token)
        if parts and parts[1] is not None and parts[0].lower() in by_lower:
            scores[by_lower[parts[0].lower()]] = parts[1]
    return scores


def shown(slot, saved: dict[str, dict]) -> tuple[str | None, float]:
    """The player and score to display for a slot: a saved lock's own record
    (when it has a score) beats whatever Sleeper's API shows right now."""
    lock = saved.get(slot.label)
    if lock and lock["score"] is not None:
        return lock["player_id"], lock["score"]
    return slot.player_id, slot.score


def disagreements(slots, saved: dict[str, dict], name=str) -> list[str]:
    """Where Sleeper's lineup doesn't match a saved lock, in words."""
    lines = []
    for s in slots:
        lock = saved.get(s.label)
        if not lock:
            continue
        if lock["player_id"] != s.player_id:
            lines.append(f"{s.label} was saved as locked with {name(lock['player_id'])}, but Sleeper "
                         f"shows {name(s.player_id) if s.player_id else 'nobody'} there. Enter keeps "
                         f"{name(lock['player_id'])}; type your locked slots to take Sleeper's lineup.")
        elif lock["score"] is not None and abs(lock["score"] - s.score) > 0.05:
            lines.append(f"{s.label} ({name(s.player_id)}) was locked at {lock['score']:.1f}, but "
                         f"Sleeper's API now says {s.score:.1f}. Using {lock['score']:.1f}.")
    return lines


def lock_slots(slots, chosen: list[str], saved: dict[str, dict],
               typed: dict[str, float] | None = None, kept: bool = False) -> list:
    """Mark the chosen slots locked and settle each one's player and score.

    In order of trust:
      * a score typed after '=' (e.g. F=27.9), saved without a game;
      * the saved lock, if it's the same player Sleeper shows in that slot;
      * the saved lock even if Sleeper shows someone else there, when the
        user kept the saved locks (Enter): a locked slot can't change in Sleeper,
        so its numbers are the ones that are off;
      * otherwise a new lock: Sleeper's player, with the slot's current score.

    Returns the new locks, so the caller can pick the game each was locked on
    (slot_scores.settle_new_lock).
    """
    typed = typed or {}
    new = []
    for s in slots:
        s.locked = s.label in chosen
        if not s.locked:
            continue
        lock = saved.get(s.label)
        if lock and (kept or lock["player_id"] == s.player_id):
            s.player_id = lock["player_id"]
            s.game_id = lock.get("game_id")
            if lock["score"] is not None:
                s.score = lock["score"]
        elif s.label not in typed:
            new.append(s)
        if s.label in typed:
            s.score, s.game_id = typed[s.label], None
    return new
