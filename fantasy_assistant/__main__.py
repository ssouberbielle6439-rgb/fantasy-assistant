"""Command-line entry point. Run from the project folder:

    python -m fantasy_assistant roster     # your league and roster
    python -m fantasy_assistant profiles   # each player's average, swing, and lock-in bars
    python -m fantasy_assistant week       # who plays which day this week
    python -m fantasy_assistant advise     # LOCK or PASS for every lineup slot (saved to a log)
    python -m fantasy_assistant review     # how the saved advice turned out
    python -m fantasy_assistant chat       # ask questions in plain English (Gemini)
    python -m fantasy_assistant note 7 PG locked him anyway   # note on saved advice #7
    python -m fantasy_assistant merge data # merge another computer's locks and advice log

This file only reads commands, asks questions, and prints. The work is done
in service.py, which returns plain data, so the web app and the AI chat can
call the same functions.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from . import service, state


def _print_warnings(warnings: list[str]) -> None:
    for w in warnings:
        print(f"  ({w})")


def _load_team() -> service.Team:
    team = service.load_team()
    for w in team.warnings:
        print(f"WARNING: {w}\n")
    return team


def cmd_roster() -> int:
    team = _load_team()
    league = team.league
    print(f"League: {league['name']}  ({league['sport'].upper()} {league['season']}, "
          f"{league['total_rosters']} teams)")
    print(f"Lineup: {', '.join(p for p in league['roster_positions'] if p != 'BN')}")
    print(f"\n{team.user['display_name']}'s roster:")
    for pid in team.player_ids:
        p = team.player(pid)
        positions = "/".join(p.get("fantasy_positions") or [p.get("position") or "?"])
        print(f"  {team.name(pid):<26} {positions:<10} {p.get('team') or 'FA':<4} "
              f"{p.get('injury_status') or ''}")
    return 0


def cmd_profiles() -> int:
    team = _load_team()
    print("Pulling game logs from Sleeper...\n")
    print(f"{'Player':<24} {'Games':>5} {'Avg':>6} {'Swing':>6} {'Plays':>6}"
          f"   Lock if at least, with 1 / 2 / 3 games left")
    failed = 0
    for pid in team.player_ids:
        view = service.player_profile(team, pid)
        _print_warnings(view.warnings)
        if view.no_logs:
            print(f"{view.name:<24}  no game logs: {view.no_logs}")
            failed += 1
            continue
        prof = view.profile
        if prof is None:
            print(f"{view.name:<24}  not enough games yet")
            continue
        bars = view.lock_bars
        print(f"{view.name:<24} {prof.games_used:>5} {prof.average:>6.1f} {prof.swing:>6.1f} "
              f"{prof.play_prob:>6.0%}   {bars[0]:5.1f} / {bars[1]:5.1f} / {bars[2]:5.1f}")

    if failed:
        print(f"\n{failed} player(s) couldn't be downloaded. Anything already downloaded "
              "is saved, so rerunning only retries the missing ones.")
    return 0


def cmd_week() -> int:
    team = _load_team()
    grid = service.week_grid(team, datetime.now())
    _print_warnings(grid.warnings)
    week, today = grid.days, grid.today

    header = "".join(f"{d.strftime('%a %m/%d'):>10}" for d in week)
    print(f"Week of {week[0]:%b %d} - {week[-1]:%b %d}   (* = today)\n")
    print(f"{'Player':<22} {'Status':<7}{header}   Left  Next game")
    for row in grid.rows:
        cells = ""
        for d in week:
            text = row.games.get(d, "-")
            if d in row.games and d == today:
                text = "*" + text
            cells += f"{text:>10}"
        next_txt = (f"{row.next_chance:.0%} to play" if row.next_chance is not None
                    else "no games left")
        if row.tipoff_note:
            next_txt += f"; {row.tipoff_note}"
        if row.playing_now:
            next_txt = f"playing now (counts only if he started it); later: {next_txt}"
        label = (row.injury_status or "") + (" IR" if row.on_ir else "")
        print(f"{row.name[:22]:<22} {label:<7}{cells}   {row.left:>4}  {next_txt}")

    counts = "".join(f"{(str(grid.playing_per_day[d]) if d >= today else ''):>10}" for d in week)
    print(f"\n{'Playing (not on IR)':<30}{counts}")
    return 0


def _ask_locks(slots, saved: dict[str, dict], whose: str | None = None) -> list:
    """Ask which slots are locked and mark them (with the right score) on `slots`.
    Returns the slots locked just now (not saved before, no score typed).
    `whose`: the other team's name, when asking about your opponent's lineup."""
    from .locks import clean, lock_slots, parse_answer, typed_scores, unlocked_by

    labels = [s.label for s in slots]
    # Saved locks are never dropped silently: if Sleeper disagrees, it was
    # already printed above, and Enter still keeps them.
    kept = [lab for lab in saved if lab in labels]
    hint = ", ".join(kept) if kept else "none"
    while True:
        which = f"Which of {whose}'s slots are locked?" if whose else "Which slots are locked?"
        answer = _read(f"\n{which} (e.g. PG F UTIL2, F=27.9 if Sleeper shows "
                       f"the wrong score, 'none', or Enter to keep: {hint}) > ")
        chosen = parse_answer(answer, labels, kept)
        if chosen is None:
            print(f"Didn't recognize that. Slot names are: {' '.join(labels)}")
            continue
        # Locks are permanent in Sleeper, so removing a saved one needs a 'yes'.
        # With piped input there's no second line, so the saved locks stay.
        removing = unlocked_by(chosen, kept)
        if removing:
            sure = _read(f"That removes {', '.join(removing)}, saved as locked. Locks are permanent "
                         f"in Sleeper, so this is usually a typo. Type yes to remove "
                         f"{'it' if len(removing) == 1 else 'them'}, or Enter to answer again > ")
            if clean(sure).lower() not in ("yes", "y"):
                continue
        return lock_slots(slots, chosen, saved, typed_scores(answer, labels),
                          kept=not clean(answer))


def _read(prompt: str) -> str:
    """input(), but the end of piped input counts as pressing Enter."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def _show_lineup(team: service.Team, lineup: service.Lineup) -> None:
    """A lineup table for the top of advise (yours, or your opponent's)."""
    from . import locks

    for s in lineup.slots:
        pid, score = locks.shown(s, lineup.saved)
        name = team.name(pid) if pid else "(empty)"
        mark = "  [saved as locked]" if s.label in lineup.saved else ""
        print(f"  {s.label:<6} {name:<24} {score:6.1f}{mark}")
    for line in lineup.notes:
        print(f"  Note: {line}")


def _change(odds: service.LockOdds) -> str:
    """'win probability +4 (54% -> 58%)'."""
    return f"win probability {odds.change:+d} ({odds.without}% -> {odds.with_it}%)"


def _win_probability(team: service.Team, lineup: service.Lineup, advice: service.Advice,
                     now: datetime) -> service.WinOdds | None:
    """Your opponent's lineup, the question about their locks, and your odds.

    Optional: with no opponent this week, or if their data fails, it prints
    one line and returns None, and advise goes on as before.
    """
    opponent, problem = service.load_opponent(team, lineup.week_no, now)
    if opponent is None:
        print(f"\n  ({problem})")
        return None
    name = opponent.team.team_name
    print(f"\n{name}'s lineup right now (Sleeper's API doesn't show their locks, so the app "
          f"asks)\n")
    _show_lineup(opponent.team, opponent.lineup)
    newly_locked = _ask_locks(opponent.lineup.slots, opponent.lineup.saved, whose=name)
    for note in service.record_locks(opponent.team, opponent.lineup, newly_locked):
        print(f"  Note: {note}")
    try:
        odds = service.win_probability(team, lineup, advice, opponent, now)
    except Exception as err:   # optional: never stop the advice over it
        print(f"\n  (Couldn't work out the win probability: {type(err).__name__}: {err}.)")
        return None
    _print_warnings(odds.warnings)
    if odds.final:
        print(f"\nFinal totals: you {odds.my_expected:.1f}, {name} {odds.their_expected:.1f}.")
        return odds
    print(f"\nWin probability: {odds.percent}% vs {name} ({service.POLICY_TEXT[odds.policy]}; "
          f"{odds.sims:,} simulated weeks)")
    print(f"  Expected totals: you {odds.my_expected:.1f}, {name} {odds.their_expected:.1f}.")
    return odds


def cmd_advise() -> int:
    from . import schedule
    from .lineup import today_line

    team = _load_team()
    now = datetime.now()
    lineup = service.current_lineup(team, now)
    _print_warnings(lineup.warnings)
    short = team.short

    print(f"Week {lineup.week_no}: your lineup right now (scores from Sleeper's box scores; "
          f"locked slots show the saved game)\n")
    _show_lineup(team, lineup)
    newly_locked = _ask_locks(lineup.slots, lineup.saved)
    for note in service.record_locks(team, lineup, newly_locked):
        print(f"  Note: {note}")

    print("\nLoading player profiles (cached game logs)...")
    advice = service.advise(team, lineup, now)
    _print_warnings(advice.warnings)

    # Week's over for this lineup: no advice to give, just the final scores.
    if advice.final_scores:
        print("\n" + "\n".join(advice.final_scores))
        _win_probability(team, lineup, advice, now)   # both totals (theirs may still be going)
        return 0

    # Your odds this week if you follow the advice below (the advice doesn't use them).
    odds = _win_probability(team, lineup, advice, now)
    lock_odds = {o.move.player_id: o for o in odds.locks} if odds else {}
    held_odds = {o.move.player_id: o for o in odds.held} if odds else {}

    # 1. Which scores in hand to lock, and in which slot.
    if advice.locks:
        print("\nRecommended locks (in this order):")
        for step in advice.locks:
            below = f"  [{service.target_label(advice.target)}]" if step.below_target else ""
            soon = f"  [{schedule.LOCK_BEFORE_TIPOFF}]" if step.before_tipoff else ""
            print(f"  - {step.instruction}{below}{soon}")
            if step.move.player_id in lock_odds:
                print(f"      {_change(lock_odds[step.move.player_id]).capitalize()} "
                      f"vs. skipping this lock.")
        print(f"  These add about {advice.total_gain:.1f} expected points to your week vs. passing.")
    else:
        print("\nNo locks recommended right now: every score in hand is worth passing on.")
    for h in advice.held:
        held = service.floor_label(advice.floor)
        print(f"  ({held[0].upper() + held[1:]}: locking {short(h.player_id)}'s "
              f"{h.score:.1f} in {h.to_label} would add about {h.gain:.1f}. Your call.)")
        if h.player_id in held_odds:
            print(f"      (Locking it anyway: {_change(held_odds[h.player_id])}. Your call.)")

    # 2. The plan for the rest of the week: which player each open slot is waiting on.
    print(f"\n{'Slot':<6} {'Now':<13} {'Score':>6}  {'Status':<7}  {'Waiting on':<13} {'Exp':>5}  "
          f"{'Depth':<5} Alternates")
    for row in advice.slots:
        name = short(row.player_id) if row.player_id else "(empty)"
        if row.expected is None:  # locked
            print(f"{row.label:<6} {name:<13} {row.score:6.1f}  {row.status}")
            continue
        alts = ", ".join(short(p) for p in row.alternates) or "none"
        target = short(row.waiting_on) if row.waiting_on else "nobody"
        print(f"{row.label:<6} {name:<13} {row.score:6.1f}  {row.status:<7}  "
              f"{target:<13} {row.expected:5.1f}  {row.depth:<5} {alts}")

    # 3. The weakest open slots are where solid scores should get locked.
    if advice.weakest:
        weakest = ", ".join(f"{label} ({exp:.1f})" for label, exp in advice.weakest)
        print(f"\nWeakest open slots this week: {weakest}. Lock solid scores here first;"
              f"\nkeep UTIL open as long as you can for late big games from anyone.")

    # 4. Today: start the planned player if he plays; otherwise a free roll.
    if advice.today:
        print("\nToday's plan for open slots:")
        for start in advice.today:
            print("  " + today_line(start.move, start.opponent, short))

    print(f"\nWaiting on = the player planned for that slot; Exp = what you'd expect to lock"
          f" from him this week.\nEach player can only be locked once, so each slot waits on"
          f" a different player.\nLOCK = lock now (steps above). PASS = keep the slot open. "
          f"WAIT = his game hasn't happened. OPEN = empty.\nPLAYING = his game is on now: "
          f"it replaces the score shown when it ends. He can move\nbetween starting slots "
          f"but not to the bench (that clears the game).")

    # 5. Save this advice so `review` can check it once the games are played.
    run_id = service.save_advice(advice, odds)
    print(f"\nSaved as advice #{run_id}. Disagree? python -m fantasy_assistant note {run_id} "
          f"[slot] your reason")
    return 0


def cmd_chat() -> int:
    """Ask about your lineup in plain English. Gemini picks which of the app's
    tools to run and explains their answers (chat.py); nothing is saved."""
    from . import chat, gemini

    try:
        settings = gemini.settings_from_env()   # before loading anything: no key, no chat
    except gemini.MissingKey as err:
        sys.exit(str(err))
    team = _load_team()   # once for the whole chat
    session = chat.ChatSession(team, settings)
    # Gemini's answers can hold characters an old Windows console can't show.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print(f"Ask about your lineup in plain English (Gemini model: {settings.model}).\n"
          f"Answers come from the app's own numbers, and nothing is saved to the advice log.\n"
          f"Press Enter on an empty line, or type quit, to stop.")
    while True:
        question = _read("\nYou > ").strip()
        if question.lower() in ("", "quit", "exit", "q"):
            return 0
        try:
            answer = session.ask(question)
        except gemini.GeminiError as err:
            print(f"  ({err})")
            if err.fatal:
                return 1
            continue
        _print_warnings(answer.warnings)
        print(f"App > {answer.text}")
        if answer.unmatched:
            print(f"  (Number check: {', '.join(answer.unmatched)} "
                  f"{'does' if len(answer.unmatched) == 1 else 'do'} not match any number the "
                  f"app gave. Treat {'it' if len(answer.unmatched) == 1 else 'them'} with care.)")
        for line in answer.labels:   # advise's label, which the answer left out
            print(f"  (Label check: {line}.)")
        if answer.tools_used:
            print(f"  (from: {', '.join(dict.fromkeys(answer.tools_used))})")


def cmd_note(args: list[str]) -> int:
    """note                      -> list this week's saved advice
    note 7 PG locked him anyway -> attach a note to run #7 (slot is optional)
    note last too passive        -> 'last' means the most recent run"""
    from . import advice_log

    entries = advice_log.load()
    if not entries:
        print("No advice saved yet. Run `advise` first.")
        return 1
    if not args:
        latest_week = entries[-1]["week_start"]
        print(f"Saved advice for the week of {latest_week}:\n")
        for e in entries:
            if e["week_start"] == latest_week:
                when = _time_text(e["time"])
                notes = f"  [{len(e['notes'])} note(s)]" if e.get("notes") else ""
                print(f"  #{e['id']:<3} {when}  {advice_log.one_line(e)}{notes}")
        print("\nAdd a note: python -m fantasy_assistant note <number or 'last'> [slot] your reason")
        return 0

    run_id = entries[-1]["id"] if args[0].lower() == "last" else _int_or_none(args[0])
    entry = next((e for e in entries if e["id"] == run_id), None)
    if entry is None:
        print(f"No saved advice #{args[0]}. Run `note` with no arguments to see the list.")
        return 1
    rest = args[1:]
    slot_names = {r["slot"].lower(): r["slot"] for r in entry["slots"]}
    slot = slot_names.get(rest[0].lower()) if rest else None
    if slot:
        rest = rest[1:]
    text = " ".join(rest).strip()
    if not text:
        print("Write the note after the number, e.g.: note 7 PG locked him anyway, he was hot")
        return 1
    advice_log.add_note(entry["id"], text, slot)
    print(f"Note added to advice #{entry['id']}" + (f" ({slot})" if slot else "") + ".")
    return 0


def _int_or_none(text: str) -> int | None:
    try:
        return int(text.lstrip("#"))
    except ValueError:
        return None


def _time_text(iso: str) -> str:
    """'2026-10-29T14:05:00' -> 'Thu 10/29 2:05pm'."""
    t = datetime.fromisoformat(iso)
    return f"{t:%a %m/%d} {t.hour % 12 or 12}:{t:%M}{'am' if t.hour < 12 else 'pm'}"


def cmd_review(args: list[str]) -> int:
    """review      -> grade this week's saved advice (or the latest week with any)
    review all  -> every week"""
    from .advice_log import FREE, PENDING, RIGHT, WRONG

    entries = service.saved_advice(all_weeks=bool(args and args[0].lower() == "all"))
    if not entries:
        print("No advice saved yet. Run `advise` first; each run is saved automatically.")
        return 1
    team = _load_team()
    result = service.review(team, entries, date.today())
    _print_warnings(result.lineup_warnings)
    print("Checking game logs on Sleeper...")
    _print_warnings(result.log_warnings)

    for e in result.entries:
        print(f"\n#{e['id']}  {_time_text(e['time'])}  (week {e['week']})")
        mine = result.calls_in_run(e["id"])
        if not mine:
            print("  nothing new (same calls as an earlier run, or no scores in hand)")
        for c in mine:
            what = f"{c.player} {c.score:.1f}" + (f" in {c.lock_slot}" if c.lock_slot else f" ({c.slot})")
            later = ", ".join(g.text() for g in c.later) or "no games left"
            repeat = f"  [repeated in #{', #'.join(map(str, c.also_in))}]" if c.also_in else ""
            print(f"  {c.kind:<4}  {what:<26} later: {later}")
            mine_too = " (your override)" if c.override else ""
            print(f"        -> {c.verdict}{mine_too}: {c.detail}{repeat}")
        for n in e.get("notes", []):
            print(f"  note{' (' + n['slot'] + ')' if n.get('slot') else ''}: {n['text']}")

    # Summary
    print("\nHow the advice did (each call counted once):")
    meaning = {"LOCK": "graded on his next game",
               "PASS": "graded on his next game; right = it beat the score",
               "HELD": "held back by the floor; right = his next game beat it"}
    total_right = total_graded = 0
    for kind in ("LOCK", "PASS", "HELD"):
        k = result.tally.get(kind)
        if not k:
            continue
        right, graded = k.get(RIGHT, 0), k.get(RIGHT, 0) + k.get(WRONG, 0)
        total_right, total_graded = total_right + right, total_graded + graded
        score = f"{right} of {graded} right ({right / graded:.0%})" if graded else "none graded yet"
        extras = [f"{k[v]} {label}" for v, label in
                  ((PENDING, "pending"), (FREE, "free (no games left)")) if k.get(v)]
        print(f"  {kind:<4}  {score}" + (f"; {', '.join(extras)}" if extras else "")
              + f"   ({meaning[kind]})")
    if total_graded:
        print(f"  Overall: {total_right} of {total_graded} graded calls right "
              f"({total_right / total_graded:.0%})")
    beat, overridden = result.overrides
    if overridden:
        print(f"  Your overrides: {beat} of {overridden} beat the app "
              "(you locked a score it said to pass on)")

    gap = result.forecast_gap
    if gap is not None:
        print(f"  Forecast check: {len(result.forecast_pairs)} finished slot(s) ended {abs(gap):.1f} "
              f"points {'above' if gap >= 0 else 'below'} the plan's Exp on average")
    n_notes = sum(len(e.get("notes", [])) for e in result.entries)
    if n_notes:
        print(f"  Notes: {n_notes} (shown under their runs)")
    print("\nEvery call is graded on the player's next game; \"slot ended at\" is shown "
          "for information only.")
    return 0


def cmd_merge(args: list[str]) -> int:
    """Merge another folder's lock files and advice log into this computer's
    state folder, e.g. `merge data` after setting STATE_DIR=notes/state."""
    if len(args) != 1:
        print("Usage: python -m fantasy_assistant merge <folder>   (e.g. merge data)")
        return 1
    source, target = state.state_dir(args[0]), state.state_dir(os.getenv("STATE_DIR"))
    if source.resolve() == target.resolve():
        print(f"{source} is already where your locks and advice log are saved. Set STATE_DIR "
              f"in .env to the folder you want them in, then merge the old folder into it.")
        return 1
    if not source.is_dir():
        print(f"No folder at {source}.")
        return 1
    print(f"Merging {source} into {target}:")
    for line in state.merge_folder(source, target) or ["nothing to merge"]:
        print(f"  {line}")
    print(f"\nThe originals in {source} weren't changed. Once this looks right, you can "
          f"delete the locks_*.json, opp_locks_*.json and advice_log.json files there.")
    return 0


def main() -> int:
    load_dotenv()
    state.use(state.state_dir(os.getenv("STATE_DIR")))  # where locks and the advice log live
    commands = {"roster": cmd_roster, "profiles": cmd_profiles, "week": cmd_week,
                "advise": cmd_advise, "chat": cmd_chat}
    with_args = {"review": cmd_review, "note": cmd_note,  # these take extra words
                 "merge": cmd_merge}
    parser = argparse.ArgumentParser(prog="fantasy_assistant")
    parser.add_argument("command", nargs="?", default="roster",
                        choices=list(commands) + list(with_args))
    parser.add_argument("args", nargs="*",
                        help="for note: <number> [slot] text; for review: all; for merge: <folder>")
    args = parser.parse_args()
    try:
        if args.command in with_args:
            return with_args[args.command](args.args)
        return commands[args.command]()
    except service.SetupError as err:
        sys.exit(str(err))


if __name__ == "__main__":
    sys.exit(main())
