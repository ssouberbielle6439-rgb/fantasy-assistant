import json
import sys
from datetime import date

from fantasy_assistant import advice_log, locks, nba_stats, schedule, sleeper, state
from fantasy_assistant.__main__ import main


def run(time, notes=(), week_start="2026-11-02", run_id=1):
    """A saved advise run, cut down to what merging looks at."""
    return {"time": time, "week": 3, "week_start": week_start, "slots": [], "locks": [],
            "held": [], "id": run_id, "notes": list(notes)}


def note(time, text):
    return {"time": time, "slot": None, "text": text}


# ---------- The STATE_DIR setting ----------

def test_missing_or_blank_state_dir_means_data():
    for setting in (None, "", "   "):
        assert state.state_dir(setting) == state.PROJECT_DIR / "data"


def test_relative_state_dir_counts_from_the_project_folder(tmp_path):
    # Runs from another folder must still find notes/state inside the project.
    assert state.state_dir("notes/state") == state.PROJECT_DIR / "notes" / "state"
    assert state.state_dir(str(tmp_path)) == tmp_path


def test_state_dir_moves_locks_and_advice_log_but_not_the_caches(tmp_path):
    synced = tmp_path / "notes" / "state"   # doesn't exist yet: the first save makes it
    caches = (nba_stats.DATA_DIR, schedule.DATA_DIR, sleeper.DATA_DIR)
    state.use(synced)
    locks.save(date(2026, 11, 2), {"PG": {"player_id": "1747", "score": 32.9}})
    advice_log.append(run("2026-11-05T09:45:28"))
    assert sorted(p.name for p in synced.iterdir()) == ["advice_log.json",
                                                        "locks_2026-11-02.json"]
    assert (nba_stats.DATA_DIR, schedule.DATA_DIR, sleeper.DATA_DIR) == caches


def test_main_reads_state_dir_before_any_command(tmp_path, monkeypatch):
    # `note` and `review` never connect to Sleeper, so the setting can't wait
    # for the Sleeper login to load .env. (`merge` with no folder just prints usage.)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "synced"))
    monkeypatch.setattr("fantasy_assistant.__main__.load_dotenv", lambda: None)  # not the real .env
    monkeypatch.setattr(sys, "argv", ["fantasy_assistant", "merge"])
    assert main() == 1
    assert locks.STATE_DIR == tmp_path / "synced"
    assert advice_log.LOG_PATH == tmp_path / "synced" / "advice_log.json"


# ---------- Merging the home PC's state into the synced folder ----------

def test_home_pc_and_laptop_logs_merge_in_time_order_with_every_note():
    laptop = [run("2026-11-05T09:45:28", run_id=1),
              run("2026-11-05T09:49:27", [note("2026-11-05T10:00:00", "laptop note")], run_id=2)]
    home = [run("2026-11-03T21:10:00", [note("2026-11-04T08:00:00", "late"),
                                        note("2026-11-03T22:00:00", "early")], run_id=1),
            run("2026-11-06T20:00:00", run_id=2)]
    merged = advice_log.merge(laptop, home)
    assert [e["time"] for e in merged] == ["2026-11-03T21:10:00", "2026-11-05T09:45:28",
                                           "2026-11-05T09:49:27", "2026-11-06T20:00:00"]
    assert [e["id"] for e in merged] == [1, 2, 3, 4]   # review relies on numbers following the clock
    assert [n["text"] for n in merged[0]["notes"]] == ["early", "late"]
    assert [n["text"] for n in merged[2]["notes"]] == ["laptop note"]


def test_a_run_in_both_logs_is_kept_once_with_both_computers_notes():
    shared = note("2026-11-05T10:00:00", "on both")
    laptop = [run("2026-11-05T09:45:28", [shared], run_id=1)]
    home = [run("2026-11-05T09:45:28", [shared, note("2026-11-05T11:00:00", "home only")],
                run_id=7)]
    merged = advice_log.merge(laptop, home)
    assert len(merged) == 1 and merged[0]["id"] == 1
    assert [n["text"] for n in merged[0]["notes"]] == ["on both", "home only"]


def test_lock_files_merge_slot_by_slot_and_flag_clashes():
    mine = {"PG": {"player_id": "1747", "score": 32.9}, "C": {"player_id": "2728", "score": 38.6}}
    theirs = {"PG": {"player_id": "1747", "score": 32.9}, "SG": {"player_id": "1830", "score": 26.4},
              "C": {"player_id": "2728", "score": 36.6}}
    merged, conflicts = locks.merge(mine, theirs)
    assert merged == {**theirs, "C": mine["C"]}   # every slot stays locked; a clash keeps mine
    assert [slot for slot, _, _ in conflicts] == ["C"]


def test_merge_folder_moves_home_pc_state_in_and_leaves_the_original_alone(tmp_path):
    home, synced = tmp_path / "data", tmp_path / "notes" / "state"
    home.mkdir()
    synced.mkdir(parents=True)
    (home / "locks_2026-11-02.json").write_text(
        json.dumps({"SG": {"player_id": "1830", "score": 26.4}}), encoding="utf-8")
    (home / "locks_2026-10-26.json").write_text("{}", encoding="utf-8")
    advice_log.save_all([run("2026-11-03T21:10:00")], home / "advice_log.json")
    (synced / "locks_2026-11-02.json").write_text(
        json.dumps({"PG": {"player_id": "1747", "score": 32.9}}), encoding="utf-8")
    advice_log.save_all([run("2026-11-05T09:45:28")], synced / "advice_log.json")
    home_before = {p.name: p.read_text(encoding="utf-8") for p in home.iterdir()}

    state.merge_folder(home, synced)

    assert json.loads((synced / "locks_2026-11-02.json").read_text(encoding="utf-8")) == {
        "PG": {"player_id": "1747", "score": 32.9}, "SG": {"player_id": "1830", "score": 26.4}}
    assert (synced / "locks_2026-10-26.json").exists()
    assert [(e["id"], e["time"]) for e in advice_log.load(synced / "advice_log.json")] == [
        (1, "2026-11-03T21:10:00"), (2, "2026-11-05T09:45:28")]
    assert {p.name: p.read_text(encoding="utf-8") for p in home.iterdir()} == home_before
