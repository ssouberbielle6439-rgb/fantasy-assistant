"""Win probability through service.py and advise, on the frozen week 2 from
test_service.py (Friday 10/30 at noon, Mobley locked in F at 27.9) against a
made-up opponent, "Rival Five". Their players are made up too, and each one
gets the real box scores of one of ours (same NBA team and positions), so
both lineups are scored from the same saved data. No network.
"""

from datetime import datetime

import pytest
import requests

from fantasy_assistant import __main__ as cli
from fantasy_assistant import advice_log, locks, service, sleeper, winprob
from fantasy_assistant.lineup import LockMove
from test_service import (CLINGAN, FRIDAY_NOON, MOBLEY, MONDAY, MURRAY, PLAYERS,  # noqa: F401
                          SMITH_JR, STARTERS, friday_advice, lineup_with_saved_locks,
                          save_mobley_lock, week)

R_GUARD, R_BIG, R_CENTER, R_WING = "8747", "8259", "8728", "8445"
RIVALS = {
    R_GUARD: {"first_name": "Rex", "last_name": "Guard", "full_name": "Rex Guard",
              "team": "DEN", "fantasy_positions": ["PG"]},
    R_BIG: {"first_name": "Bo", "last_name": "Big", "full_name": "Bo Big",
            "team": "CLE", "fantasy_positions": ["PF", "C"]},
    R_CENTER: {"first_name": "Cy", "last_name": "Center", "full_name": "Cy Center",
               "team": "POR", "fantasy_positions": ["C"]},
    R_WING: {"first_name": "Wes", "last_name": "Wing", "full_name": "Wes Wing",
             "team": "HOU", "fantasy_positions": ["PF"]},
}
RIVAL_ROSTER = {"roster_id": 2, "owner_id": "rival-user",
                "players": [R_GUARD, R_BIG, R_CENTER, R_WING], "reserve": []}
RIVAL_STARTERS = [R_GUARD, R_BIG, R_CENTER, R_WING]   # PG, F, C, UTIL
SIMS = 2000   # fewer than advise's 10,000, to keep the tests quick


def add_opponent(week, monkeypatch):
    """Put Rival Five in our matchup (matchup 3); another matchup is going on too."""
    from test_service import ROSTER

    week.box_scores_of.update({R_GUARD: MURRAY, R_BIG: MOBLEY, R_CENTER: CLINGAN,
                               R_WING: SMITH_JR})
    monkeypatch.setattr(sleeper, "get_players", lambda sport="nba": {**PLAYERS, **RIVALS})
    monkeypatch.setattr(sleeper, "get_rosters", lambda league_id: [
        dict(ROSTER), dict(RIVAL_ROSTER), {"roster_id": 3, "owner_id": "x", "players": []}])
    monkeypatch.setattr(sleeper, "get_league_users", lambda league_id: [
        {"user_id": "demo-user", "display_name": "Demo"},
        {"user_id": "rival-user", "display_name": "rival", "metadata": {"team_name": "Rival Five"}}])
    monkeypatch.setattr(sleeper, "get_matchups", lambda league_id, week_no: [
        {"roster_id": 3, "matchup_id": 4, "starters": [], "starters_points": []},
        {"roster_id": 1, "matchup_id": 3, "starters": STARTERS,
         "starters_points": week.sleeper_points},
        {"roster_id": 2, "matchup_id": 3, "starters": RIVAL_STARTERS,
         "starters_points": week.sleeper_points}])


@pytest.fixture
def rival(week, monkeypatch):
    add_opponent(week, monkeypatch)
    return week


def odds_at(now, policy=None, opponent_locks=None, sims=SIMS):
    team = service.load_team()
    lineup = lineup_with_saved_locks(team, now)
    advice = service.advise(team, lineup, now)
    if opponent_locks:
        locks.save(MONDAY, opponent_locks, locks.OPPONENT)
    opponent, problem = service.load_opponent(team, lineup.week_no, now)
    assert problem is None
    service.keep_saved_locks(opponent.lineup)
    return team, lineup, advice, opponent, service.win_probability(
        team, lineup, advice, opponent, now, policy, sims=sims)


# ---------- The opponent ----------

def test_opponent_is_the_other_team_in_our_matchup_scored_like_ours(rival):
    save_mobley_lock()
    team = service.load_team()
    opponent, problem = service.load_opponent(team, 2, FRIDAY_NOON)
    assert problem is None and opponent.team.team_name == "Rival Five"
    assert opponent.team.player_ids == RIVAL_STARTERS
    # Same box scores as ours, but Bo Big (Mobley's games) isn't locked: his
    # latest complete game counts, 17.6. Our saved lock isn't theirs.
    assert [(s.label, s.score, s.locked) for s in opponent.lineup.slots] == [
        ("PG", 23.4, False), ("F", 17.6, False), ("C", 22.8, False), ("UTIL", 14.4, False)]
    assert opponent.lineup.saved == {}


def test_opponents_locks_are_saved_in_their_own_file(rival):
    save_mobley_lock()
    team = service.load_team()
    opponent, _ = service.load_opponent(team, 2, FRIDAY_NOON)
    c = opponent.lineup.slots[2]
    c.locked = True
    service.record_locks(opponent.team, opponent.lineup, [c])
    # A new lock takes the game matching Sleeper's slot number, as yours do:
    # Sleeper shows 16.0 in C here (Cy Center's Tuesday game), not Thursday's 22.8.
    assert locks.load(MONDAY, locks.OPPONENT) == {
        "C": {"player_id": R_CENTER, "score": 16.0, "game_id": c.game_id}}
    assert set(locks.load(MONDAY)) == {"F"}   # yours untouched


def test_no_opponent_this_week_is_one_sentence(week):
    # The frozen week's matchup has no matchup_id (a bye, or no matchups yet).
    assert service.load_opponent(service.load_team(), 2, FRIDAY_NOON) == (
        None, "No opponent this week, so no win probability.")


def test_opponent_data_failing_is_one_sentence(rival, monkeypatch):
    team = service.load_team()

    def down(league_id):
        raise requests.ConnectionError("Sleeper is down")
    monkeypatch.setattr(sleeper, "get_rosters", down)
    opponent, problem = service.load_opponent(team, 2, FRIDAY_NOON)
    assert opponent is None
    assert problem == ("Couldn't load your opponent's lineup (ConnectionError: Sleeper is down), "
                       "so no win probability this run.")


# ---------- Win probability ----------

def test_friday_noon_odds_are_reproducible_and_each_lock_change_adds_up(rival):
    save_mobley_lock()
    *_, odds = odds_at(FRIDAY_NOON)
    *_, again = odds_at(FRIDAY_NOON)
    assert odds == again
    assert (odds.opponent, odds.policy, odds.final, odds.sims) == ("Rival Five", "smart", False, SIMS)
    assert 0 <= odds.percent <= 100
    [clingan] = odds.locks   # advise's one recommended lock: Clingan in C at 22.8
    assert clingan.move.player_id == CLINGAN and clingan.with_it == odds.percent
    assert clingan.change == clingan.with_it - clingan.without   # from the rounded percents


def test_the_simulated_week_follows_advises_plan(rival):
    # Your version of the week is advise's: the recommended locks, then each
    # open slot waiting on the player advise's plan gives it.
    save_mobley_lock()
    team, lineup, advice, *_ = odds_at(FRIDAY_NOON)
    week = service._app_week(lineup.slots, [s.move for s in advice.locks], advice, team.positions)
    assert {s.label: s.player_id for s in week.slots if not s.locked} == advice.plan.assignment
    assert [(s.label, s.score) for s in week.slots if s.locked] == [("F", 27.9), ("C", 22.8)]


def test_with_the_floor_off_your_average_total_is_the_plans(rival):
    save_mobley_lock()
    team, lineup, advice, opponent, _ = odds_at(FRIDAY_NOON)
    week = service._app_week(lineup.slots, [s.move for s in advice.locks], advice, team.positions)
    expected = 27.9 + 22.8 + advice.plan.total
    totals = [sum(w[0].values()) for w in winprob.simulated_weeks(
        [week], advice.games_left, None, 20000, winprob.SEED)]
    average = sum(totals) / len(totals)
    spread = (sum((t - average) ** 2 for t in totals) / (len(totals) - 1)) ** 0.5
    assert abs(average - expected) < 4 * spread / len(totals) ** 0.5


def test_a_passive_opponent_keeps_the_locks_you_entered_and_their_last_games(rival):
    save_mobley_lock()
    *_, smart = odds_at(FRIDAY_NOON)
    *_, opponent, passive = odds_at(FRIDAY_NOON, "passive",
                                    {"C": {"player_id": R_CENTER, "score": 22.8, "game_id": None}})
    assert passive.policy == "passive" and passive.percent != smart.percent
    week = service._passive_week(opponent.lineup.slots)
    assert [(s.label, s.player_id, s.locked, s.policy) for s in week.slots] == [
        ("PG", R_GUARD, False, "passive"), ("F", R_BIG, False, "passive"),
        ("C", R_CENTER, True, "passive"), ("UTIL", R_WING, False, "passive")]


def test_an_unknown_opponent_policy_falls_back_to_smart_with_a_warning(rival, monkeypatch):
    monkeypatch.setenv("OPPONENT_POLICY", "lazy")
    save_mobley_lock()
    *_, odds = odds_at(FRIDAY_NOON)
    assert odds.policy == "smart"
    assert "OPPONENT_POLICY='lazy' isn't smart or passive, so using smart" in odds.warnings


def test_a_held_lock_shows_its_change_if_you_take_it_anyway(rival):
    # This small roster never gets a HELD on its own, so one is added to the
    # real advice: Smith Jr.'s 14.4 in UTIL, held back by the floor.
    save_mobley_lock()
    team, lineup, advice, opponent, _ = odds_at(FRIDAY_NOON)
    advice.held = [LockMove(SMITH_JR, "UTIL", "UTIL", 14.4, 0.5)]
    odds = service.win_probability(team, lineup, advice, opponent, FRIDAY_NOON, sims=SIMS)
    [held] = odds.held
    assert held.without == odds.percent       # following the advice passes on it
    assert held.change == held.with_it - held.without


def test_week_done_shows_both_final_totals_without_simulating(rival, monkeypatch):
    save_mobley_lock()
    sunday = datetime(2026, 11, 1, 12, 0)   # nobody here plays on Sunday
    rival.at(sunday)
    monkeypatch.setattr(winprob, "simulate", lambda *a, **k: pytest.fail("simulated"))
    team, lineup, advice, opponent, odds = odds_at(sunday)
    assert advice.final_scores and odds.final and odds.percent is None
    assert odds.my_expected == pytest.approx(sum(s.score for s in lineup.slots))
    assert odds.their_expected == pytest.approx(sum(s.score for s in opponent.lineup.slots))


# ---------- advise ----------

class FridayNoon(datetime):
    @classmethod
    def now(cls, tz=None):
        return FRIDAY_NOON


class SundayNoon(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 1, 12, 0)


def run_advise(monkeypatch, capsys, *typed, clock=FridayNoon):
    lines = iter(typed)

    def fake_input(prompt):
        print(prompt)
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(cli, "datetime", clock)
    monkeypatch.setattr(winprob, "SIMULATIONS", SIMS)
    assert cli.cmd_advise() == 0
    return capsys.readouterr().out


def lock_block(out):
    """advise's "Recommended locks" lines, without the win probability ones."""
    block = out.split("Recommended locks")[1].split("\n\n")[0]
    return [line for line in block.splitlines() if "Win probability" not in line]


def test_advise_shows_the_win_probability_before_the_locks_and_saves_it(rival, monkeypatch,
                                                                       capsys):
    save_mobley_lock()
    out = run_advise(monkeypatch, capsys, "", "C")   # Enter for yours; their C is locked
    assert "Which of Rival Five's slots are locked?" in out
    head = out.index("Win probability: ")
    assert head < out.index("Recommended locks")
    assert "vs Rival Five (they play smart: the app's own advice, with your target and floor;" in out
    [line] = [x for x in out.splitlines() if "vs. skipping this lock." in x]
    words = line.split()   # "Win probability +4 (54% -> 58%) vs. skipping this lock."
    change, before, after = int(words[2]), int(words[3][1:-1]), int(words[5][:-2])
    assert change == after - before
    assert out[head:].split("%")[0] == f"Win probability: {after}"
    assert locks.load(MONDAY, locks.OPPONENT)["C"]["player_id"] == R_CENTER
    [entry] = advice_log.load()
    assert entry["win_probability"]["percent"] == after
    assert entry["win_probability"]["opponent"] == "Rival Five"


def test_advise_without_an_opponent_prints_the_same_advice_plus_one_line(rival, week,
                                                                        monkeypatch, capsys):
    save_mobley_lock()
    with_odds = run_advise(monkeypatch, capsys, "", "")
    monkeypatch.setattr(sleeper, "get_matchups", lambda league_id, week_no: [
        {"roster_id": 1, "starters": STARTERS, "starters_points": week.sleeper_points}])
    without = run_advise(monkeypatch, capsys, "", "")
    assert lock_block(without) == lock_block(with_odds)   # the advice doesn't change
    assert "Win probability" not in without
    assert [x for x in without.splitlines() if "win probability" in x.lower()] == [
        "  (No opponent this week, so no win probability.)"]
    assert "Saved as advice #2." in without


def test_advise_when_the_opponents_data_fails_prints_one_line(rival, monkeypatch, capsys):
    save_mobley_lock()

    def down(league_id):
        raise requests.ConnectionError("Sleeper is down")
    monkeypatch.setattr(sleeper, "get_league_users", down)
    out = run_advise(monkeypatch, capsys, "")
    assert [x for x in out.splitlines() if "opponent" in x.lower()] == [
        "  (Couldn't load your opponent's lineup (ConnectionError: Sleeper is down), "
        "so no win probability this run.)"]
    assert "Lock D. Clingan in C (22.8)." in out and "Saved as advice #1." in out


def test_advise_when_the_weeks_done_prints_both_final_totals(rival, monkeypatch, capsys):
    save_mobley_lock()
    rival.at(datetime(2026, 11, 1, 12, 0))   # nobody here plays on Sunday
    monkeypatch.setattr(winprob, "simulate", lambda *a, **k: pytest.fail("simulated"))
    out = run_advise(monkeypatch, capsys, "", "", clock=SundayNoon)
    assert "Week's done: final scores" in out
    final = out.split("Final totals: ")[1].splitlines()[0]
    mine = float(out.split("Total")[1].split()[0])
    assert final.startswith(f"you {mine:.1f}, Rival Five ")
    assert "Win probability" not in out
