"""Remember which lineup slots are locked this week.

Sleeper's API shows each slot's score but not whether it's locked, so the
app asks once and saves the answer in data/ for the rest of the week.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _path(week_start: date) -> Path:
    return DATA_DIR / f"locks_{week_start.isoformat()}.json"


def load(week_start: date) -> dict[str, str]:
    """Saved locks for the week: slot label -> player id locked there."""
    path = _path(week_start)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(week_start: date, locks: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _path(week_start).write_text(json.dumps(locks, indent=2), encoding="utf-8")


def parse_answer(answer: str, labels: list[str], saved: list[str]) -> list[str] | None:
    """Turn the user's reply into a list of locked slot labels.

    ''      -> keep the saved locks
    'none'  -> nothing locked
    'pg f util2' -> those slots (case doesn't matter)
    Returns None if the reply has a slot name that doesn't exist.
    """
    # Windows PowerShell 5.1 starts piped text with an invisible byte-order mark
    # (﻿), which would turn "PG" into an unknown slot name.
    text = answer.replace("﻿", "").strip()
    if not text:
        return saved
    if text.lower() == "none":
        return []
    by_lower = {label.lower(): label for label in labels}
    chosen = []
    for token in text.replace(",", " ").split():
        label = by_lower.get(token.lower())
        if label is None:
            return None
        if label not in chosen:
            chosen.append(label)
    return chosen
