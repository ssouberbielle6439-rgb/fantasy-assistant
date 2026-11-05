"""Where the season record lives: the weekly lock files and the advice log.

These are the only files in data/ worth keeping. Everything else there is a
cache (game logs, player list, schedule) that can be downloaded again, so the
caches always stay in data/.

By default the season record sits in data/ too. Set STATE_DIR in .env to keep
it somewhere else; Santiago uses STATE_DIR=notes/state, a folder in his private
notes repo, so his locks and advice log sync between his two computers through
GitHub. A relative path counts from the project folder, not from wherever the
command was run.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import advice_log, locks

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DIR = PROJECT_DIR / "data"


def state_dir(setting: str | None) -> Path:
    """The folder named by STATE_DIR's value; data/ when it's missing or blank."""
    if not setting or not setting.strip():
        return DEFAULT_DIR
    path = Path(setting.strip())
    return path if path.is_absolute() else PROJECT_DIR / path


def use(folder: Path) -> None:
    """Point the lock files and the advice log at `folder`."""
    locks.STATE_DIR = folder
    advice_log.LOG_PATH = folder / "advice_log.json"


def merge_folder(source: Path, target: Path) -> list[str]:
    """Merge another computer's lock files and advice log (in `source`) into
    `target`. Nothing in `source` is changed. Returns what happened, in words.

    The advice log keeps every run and every note, in time order (see
    advice_log.merge). Lock files are merged slot by slot (see locks.merge).
    """
    report = []
    for lock_file in sorted(source.glob("locks_*.json")):
        theirs = json.loads(lock_file.read_text(encoding="utf-8"))
        mine_file = target / lock_file.name
        mine = json.loads(mine_file.read_text(encoding="utf-8")) if mine_file.exists() else {}
        merged, conflicts = locks.merge(mine, theirs)
        target.mkdir(parents=True, exist_ok=True)
        mine_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        report.append(f"{lock_file.name}: {len(merged)} locked slot(s)")
        report += [f"  {slot}: kept {keep}, not {other}. Check it in Sleeper's app."
                   for slot, keep, other in conflicts]

    their_log = source / "advice_log.json"
    if their_log.exists():
        target_log = target / "advice_log.json"
        mine, theirs = advice_log.load(target_log), advice_log.load(their_log)
        merged = advice_log.merge(mine, theirs)
        advice_log.save_all(merged, target_log)
        report.append(f"advice_log.json: {len(mine)} + {len(theirs)} runs -> {len(merged)} "
                      f"(duplicates counted once), renumbered in time order")
    return report
