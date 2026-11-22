"""service.py, run on a frozen week built from the saved box scores in
tests/data/ (no network): week 2, Mon 10/26 - Sun 11/01, with Murray,
Mobley, Smith Jr. and Clingan, as it stood at a chosen moment.

The league, roster and matchup are made up (no real usernames or league
ids, since the repo is public); the box scores and schedule are real.
"""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from fantasy_assistant import advice_log, locks, schedule, service, sleeper, sleeper_stats
from fantasy_assistant.lineup import LockMove
from fantasy_assistant.lockin import value_of_waiting
from test_sleeper_stats import LEAGUE

DATA = Path(__file__).parent / "data"
MURRAY, MOBLEY, SMITH_JR, CLINGAN, ROOKIE = "1747", "2259", "2445", "2728", "9999"
MOBLEY_VS_WAS = "1393723776029425664"   # Wed 10/28, 27.9
MONDAY = date(2026, 10, 26)
FRIDAY_NOON = datetime(2026, 10, 30, 12, 0)
PLAYERS_AS_OF = datetime(2026, 10, 30, 8, 15)   # when the player list was "downloaded"

PLAYERS = {
    MURRAY: {"first_name": "Jamal", "last_name": "Murray", "full_name": "Jamal Murray",
             "team": "DEN", "fantasy_positions": ["PG"]},
    MOBLEY: {"first_name": "Evan", "last_name": "Mobley", "full_name": "Evan Mobley",
             "team": "CLE", "fantasy_positions": ["PF", "C"]},
    SMITH_JR: {"first_name": "Jabari", "last_name": "Smith Jr.", "full_name": "Jabari Smith Jr.",
               "team": "HOU", "fantasy_positions": ["PF"]},
    CLINGAN: {"first_name": "Donovan", "last_name": "Clingan", "full_name": "Donovan Clingan",
              "team": "POR", "fantasy_positions": ["C"]},
    ROOKIE: {"first_name": "Demo", "last_name": "Rookie", "full_name": "Demo Rookie",
             "team": "DEN", "fantasy_positions": ["PG"]},   # no box scores yet
}
LEAGUE_INFO = {"league_id": "demo-league", "name": "Demo League", "sport": "nba",
               "season": "2026", "total_rosters": 12, "scoring_settings": LEAGUE,
               "roster_positions": ["PG", "F", "C", "UTIL", "BN", "BN"]}
ROSTER = {"roster_id": 1, "owner_id": "demo-user",
          "players": [MURRAY, MOBLEY, CLINGAN, SMITH_JR, ROOKIE], "reserve": []}
STARTERS = [MURRAY, MOBLEY, CLINGAN, SMITH_JR]


class FrozenWeek:
    """Serves Sleeper's answers for week 2 as they stood at `now`: games on
    earlier days are complete, today's have `today_status`, later ones are
    pre_game. Box scores of games not complete yet are ignored by the app,
    as they are for real."""

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch, self.tmp_path = monkeypatch, tmp_path
        self.rows = json.loads((DATA / "sleeper_schedule_2026.json").read_text(encoding="utf-8"))
        # Sleeper's slot numbers: Mobley's latest game in his locked F, and
        # Clingan's Tuesday game in C (stale); PG and UTIL agree with ours.
        self.sleeper_points = [23.4, 17.6, 16.0, 14.4]
        self.failing: set[str] = set()   # player ids whose Sleeper stats fail
        monkeypatch.delenv("LOCK_TARGET_POINTS", raising=False)
        monkeypatch.setenv("SLEEPER_USERNAME", "demo")
        monkeypatch.setenv("SLEEPER_LEAGUE_ID", "demo-league")
        monkeypatch.setattr(sleeper, "get_user", lambda name: {"user_id": "demo-user",
                                                               "display_name": "Demo"})
        monkeypatch.setattr(sleeper, "get_league", lambda league_id: dict(LEAGUE_INFO))
        monkeypatch.setattr(sleeper, "get_rosters", lambda league_id: [dict(ROSTER)])
        monkeypatch.setattr(sleeper, "get_players", lambda sport="nba": PLAYERS)
        monkeypatch.setattr(sleeper, "players_downloaded", lambda sport="nba": PLAYERS_AS_OF)
        monkeypatch.setattr(sleeper, "get_state", lambda sport="nba": {"week": 2})
        monkeypatch.setattr(sleeper, "get_matchups", lambda league_id, week: [
            {"roster_id": 1, "starters": STARTERS, "starters_points": self.sleeper_points}])
        monkeypatch.setattr(schedule, "season_games", lambda season: [
            schedule.Game(r["game_id"], date.fromisoformat(r["date"]), r["home"]["team"],
                          r["away"]["team"]) for r in self.rows])
        monkeypatch.setattr(sleeper_stats, "_get_json", self._get_json)
        self.at(FRIDAY_NOON)

    def at(self, now: datetime, today_status: str = "pre_game") -> "FrozenWeek":
        """Move the clock. Starts a fresh stats cache, since cached statuses
        never go backwards (as in the app)."""
        self.now, self.today_status = now, today_status
        cache = self.tmp_path / f"gamelogs_{now:%m%d%H%M}_{today_status}"
        self.monkeypatch.setattr(sleeper_stats, "DATA_DIR", cache)
        return self

    def status(self, day: str) -> str:
        d = date.fromisoformat(day)
        today = self.now.date()
        return "complete" if d < today else self.today_status if d == today else "pre_game"

    def _get_json(self, path: str):
        if path.startswith("/schedule/"):
            return [dict(r, status=self.status(r["date"])) for r in self.rows]
        player_id = path.split("/")[-1].split("?")[0]
        if player_id in self.failing:
            raise sleeper_stats.SleeperStatsUnavailable("couldn't reach Sleeper")
        sample = DATA / f"sleeper_player_{player_id}_2026.json"
        # Last season's request gets the same games: a stand-in, so each
        # player has enough games for a profile.
        return json.loads(sample.read_text(encoding="utf-8")) if sample.exists() else {}


@pytest.fixture
def week(monkeypatch, tmp_path):
    return FrozenWeek(monkeypatch, tmp_path)


def save_mobley_lock():
    """Mobley was locked in F on Wednesday's 27.9 (before Thursday's 17.6)."""
    locks.save(MONDAY, {"F": {"player_id": MOBLEY, "score": 27.9, "game_id": MOBLEY_VS_WAS}})


def lineup_with_saved_locks(team, now):
    lineup = service.current_lineup(team, now)
    service.keep_saved_locks(lineup)
    return lineup


# ---------- Your team ----------

def test_load_team_reads_the_league_roster_and_players(week):
    team = service.load_team()
    assert team.player_ids == [MURRAY, MOBLEY, CLINGAN, SMITH_JR, ROOKIE]
    assert (team.name(MURRAY), team.short(SMITH_JR)) == ("Jamal Murray", "J. Smith Jr.")
    assert team.positions[MOBLEY] == {"PF", "C"}
    assert team.warnings == []


def test_load_team_without_env_settings_raises_setup_error(week, monkeypatch):
    monkeypatch.delenv("SLEEPER_LEAGUE_ID")
    with pytest.raises(service.SetupError, match="SLEEPER_LEAGUE_ID"):
        service.load_team()


def test_unknown_scoring_rule_is_a_warning_not_a_print(week, monkeypatch, capsys):
    monkeypatch.setitem(LEAGUE_INFO, "scoring_settings", dict(LEAGUE, flagrant_2=-5.0))
    team = service.load_team()
    assert "flagrant_2" in team.warnings[0]
    assert capsys.readouterr().out == ""


# ---------- The current lineup ----------

def test_current_lineup_scores_slots_from_box_scores_and_keeps_the_saved_lock(week):
    save_mobley_lock()
    team = service.load_team()
    lineup = lineup_with_saved_locks(team, FRIDAY_NOON)
    assert lineup.week_no == 2 and lineup.days[0] == MONDAY
    assert [(s.label, s.score, s.locked) for s in lineup.slots] == [
        ("PG", 23.4, False),    # Murray's Wednesday game
        ("F", 27.9, True),      # Mobley's saved lock, though his latest game was 17.6
        ("C", 22.8, False),     # Clingan's Thursday game, not Sleeper's stale 16.0
        ("UTIL", 14.4, False),  # Smith Jr.'s Wednesday game
    ]
    assert any("was locked at 27.9, but Sleeper's API now says 17.6" in n for n in lineup.notes)
    assert any(n.startswith("C (D. Clingan): our score 22.8, Sleeper's 16.0") for n in lineup.notes)


def test_stats_failure_is_a_warning_and_the_slot_keeps_sleepers_number(week):
    week.failing.add(MURRAY)
    lineup = service.current_lineup(service.load_team(), FRIDAY_NOON)
    assert lineup.warnings == ["Sleeper stats failed for Jamal Murray: couldn't reach Sleeper; "
                               "using Sleeper's slot score"]
    assert lineup.slots[0].score == 23.4


def test_record_locks_saves_the_lock_file_with_the_game_sleeper_shows(week):
    team = service.load_team()
    lineup = service.current_lineup(team, FRIDAY_NOON)
    pg = lineup.slots[0]
    pg.locked = True                   # Murray locked at PG (Sleeper shows 23.4)
    assert service.record_locks(team, lineup, [pg]) == []
    assert locks.load(MONDAY)["PG"]["score"] == 23.4


# ---------- Advice ----------

def friday_advice(week):
    save_mobley_lock()
    team = service.load_team()
    lineup = lineup_with_saved_locks(team, FRIDAY_NOON)
    return team, service.advise(team, lineup, FRIDAY_NOON)


def test_friday_noon_advice_locks_clingan_in_c_since_nobody_else_can_play_it(week, capsys):
    team, advice = friday_advice(week)
    # 22.8 is under Friday's 23 floor, but with Mobley locked in F nobody else
    # can fill C, and Clingan's Saturday game is worth less (about 18.8).
    [step] = advice.locks
    assert (step.move.player_id, step.move.to_label, step.instruction) == (
        CLINGAN, "C", "Lock D. Clingan in C (22.8).")
    assert step.below_target and not step.before_tipoff   # he plays Saturday
    assert advice.held == [] and advice.floor == 23.0 and advice.target == 25.0
    assert [(r.label, r.status, r.waiting_on) for r in advice.slots] == [
        ("PG", "PASS", MURRAY), ("F", "LOCKED", None), ("C", "LOCK", None),
        ("UTIL", "PASS", SMITH_JR)]
    # Murray and Smith Jr. play tonight: each stays in his slot for it.
    assert [(t.move.label, t.move.player_id, t.opponent) for t in advice.today] == [
        ("PG", MURRAY, "@PHX"), ("UTIL", SMITH_JR, "@DAL")]
    assert advice.weakest == [("PG", advice.slots[0].expected)]
    assert capsys.readouterr().out == ""   # the service never prints


def test_week_values_are_the_lock_in_math_on_each_players_games_left(week):
    _, advice = friday_advice(week)
    for pid, games in advice.games_left.items():
        assert advice.values[pid] == value_of_waiting([g.forecast for g in games])
    assert [g.day.day for g in advice.games_left[SMITH_JR]] == [30, 31]   # Fri, Sat


def test_save_advice_writes_the_log_entry(week):
    _, advice = friday_advice(week)
    run_id = service.save_advice(advice)
    [entry] = advice_log.load()
    assert entry["id"] == run_id
    assert entry["locks"] == [{"player_id": CLINGAN, "from": "C", "to": "C", "score": 22.8,
                               "gain": advice.locks[0].move.gain}]
    assert entry["slots"][0] == {"slot": "PG", "player_id": MURRAY, "score": 23.4,
                                 "status": "PASS", "waiting_on": MURRAY,
                                 "expected": round(advice.slots[0].expected, 1)}
    assert entry["games_left"][MURRAY] == ["2026-10-30"]


def test_sunday_noon_week_is_done_so_only_final_scores(week):
    save_mobley_lock()
    week.at(datetime(2026, 11, 1, 12, 0))   # nobody here plays on Sunday
    team = service.load_team()
    advice = service.advise(team, lineup_with_saved_locks(team, week.now), week.now)
    assert advice.final_scores[0] == "Week's done: final scores"
    assert advice.locks == [] and advice.slots == [] and advice.log_entry == {}


def test_player_playing_now_shows_as_playing(week):
    save_mobley_lock()
    week.at(datetime(2026, 10, 30, 20, 30), today_status="in_game")
    team = service.load_team()
    advice = service.advise(team, lineup_with_saved_locks(team, week.now), week.now)
    assert [r.status for r in advice.slots if r.label in ("PG", "UTIL")] == ["PLAYING", "PLAYING"]
    assert [t.move.live for t in advice.today] == [True, True]


def test_game_log_fallback_and_skip_come_back_as_warnings_in_order(week, monkeypatch):
    # Sleeper fails for the rookie, and NBA.com can't match his name.
    week.failing.add(ROOKIE)
    monkeypatch.setattr(service.gamelogs.nba_stats, "find_nba_id", lambda name: None)
    profiles, warnings = service.build_profiles(service.load_team(), [ROOKIE, MURRAY])
    assert list(profiles) == [MURRAY]
    assert warnings == [
        "Sleeper stats failed for Demo Rookie: couldn't reach Sleeper; using NBA.com",
        "skipping Demo Rookie: couldn't match the name to NBA.com "
        "(add it to NAME_OVERRIDES in nba_stats.py)"]


# ---------- One lock decision ----------

def test_lock_decision_matches_advise_for_the_score_in_hand(week):
    save_mobley_lock()
    call = service.lock_decision(service.load_team(), CLINGAN, 22.8, FRIDAY_NOON)
    assert (call.verdict, call.slot, call.instruction) == ("LOCK", "C", "Lock D. Clingan in C (22.8).")
    assert call.bar == value_of_waiting([g.forecast for g in call.games_left])
    assert "worth about 18.8 on their own" in call.reason


def test_lock_decision_big_score_locks_and_small_score_passes(week):
    save_mobley_lock()
    team = service.load_team()
    big = service.lock_decision(team, MURRAY, 40.0, FRIDAY_NOON)
    small = service.lock_decision(team, MURRAY, 10.0, FRIDAY_NOON)
    assert (big.verdict, big.slot) == ("LOCK", "PG") and big.gain > 0
    assert (small.verdict, small.slot) == ("PASS", "PG")
    assert big.bar == small.bar   # the player-only bar doesn't depend on the score


def test_lock_decision_mid_game_treats_the_game_as_finished(week):
    # Friday night, Murray's last game of the week is on. "He has 35": that
    # game is the one asked about, so he has no games left, and locking is free.
    save_mobley_lock()
    week.at(datetime(2026, 10, 30, 20, 30), today_status="in_game")
    call = service.lock_decision(service.load_team(), MURRAY, 35.0, week.now)
    assert (call.verdict, call.slot, call.bar, call.games_left) == ("LOCK", "PG", 0.0, [])
    assert "he has no games left" in call.reason


def test_lock_decision_for_a_locked_or_bench_player(week):
    save_mobley_lock()
    team = service.load_team()
    assert service.lock_decision(team, MOBLEY, 30.0, FRIDAY_NOON).verdict == "ALREADY_LOCKED"
    assert service.lock_decision(team, ROOKIE, 30.0, FRIDAY_NOON).verdict == "NOT_STARTING"


def test_lock_decision_when_the_week_is_done(week):
    week.at(datetime(2026, 11, 1, 12, 0))
    call = service.lock_decision(service.load_team(), CLINGAN, 11.4, week.now)
    assert call.verdict == "WEEK_DONE"


def test_lock_below_the_target_carries_advises_label(week):
    # advise prints "[below your 25 target]" for Clingan's 22.8; the chat
    # answer has to say it too (Santiago, 11/22: Brooks at 23.2 didn't).
    save_mobley_lock()
    team = service.load_team()
    low = service.lock_decision(team, CLINGAN, 22.8, FRIDAY_NOON)
    assert (low.verdict, low.label) == ("LOCK", "below your 25 target")
    assert low.reason.startswith("Lock D. Clingan's 22.8 in C (below your 25 target): it adds")
    high = service.lock_decision(team, CLINGAN, 26.0, FRIDAY_NOON)
    assert (high.verdict, high.label) == ("LOCK", None)
    assert "target" not in high.reason


def test_lock_held_by_the_floor_carries_the_floor_label(week, monkeypatch):
    # This small roster never gets a HELD (the floor only holds a lock back
    # when someone else can fill the slot), so advise's result is stubbed.
    held = LockMove(SMITH_JR, "UTIL", "UTIL", 21.0, 1.5)
    monkeypatch.setattr(service, "_advise", lambda team, lineup, now, what_if=None:
                        service.Advice([], held=[held], floor=23.0, values={SMITH_JR: 19.6}))
    call = service.lock_decision(service.load_team(), SMITH_JR, 21.0, FRIDAY_NOON)
    assert (call.verdict, call.slot, call.label) == (
        "HELD", "UTIL", "held back by today's 23-point floor")


# ---------- Profiles and the week grid ----------

def test_player_profile_has_lock_bars_for_1_2_3_games_left(week):
    view = service.player_profile(service.load_team(), MURRAY)
    assert view.profile.games_used == 4   # Mon, Wed this season + the stand-in last season
    game = view.profile.forecast()
    assert view.lock_bars == [value_of_waiting([game] * n) for n in (1, 2, 3)]
    assert view.lock_bars == sorted(view.lock_bars)   # more games left, higher bar


def test_player_profile_without_games_has_no_profile(week):
    view = service.player_profile(service.load_team(), ROOKIE)
    assert (view.profile, view.lock_bars, view.no_logs) == (None, [], None)


def test_week_grid_friday_noon(week):
    grid = service.week_grid(service.load_team(), FRIDAY_NOON)
    smith = next(r for r in grid.rows if r.player_id == SMITH_JR)
    assert list(smith.games) == [date(2026, 10, d) for d in (26, 28, 30, 31)]
    assert (smith.left, smith.tipoff_note, smith.playing_now) == (
        2, "plays today, lock before tip-off", False)
    assert grid.playing_per_day[date(2026, 10, 30)] == 3   # Murray, Smith Jr., the rookie
    assert grid.warnings == []
    assert smith.next_game == date(2026, 10, 30)   # tonight's game hasn't tipped off


# ---------- Injuries ----------

def test_injury_report_gives_each_players_status_and_chance_to_play_his_next_game(
        week, monkeypatch):
    monkeypatch.setitem(PLAYERS[MURRAY], "injury_status", "Questionable")
    monkeypatch.setitem(PLAYERS[CLINGAN], "injury_status", "Out")
    report = service.injury_report(service.load_team(), FRIDAY_NOON)
    rows = {r.player_id: (r.status, r.chance, r.next_game, r.opponent) for r in report.rows}
    assert rows[MURRAY] == ("Questionable", 0.65, date(2026, 10, 30), "@PHX")
    assert rows[CLINGAN] == ("Out", 0.0, date(2026, 10, 31), "@UTA")   # out for Saturday only
    assert rows[SMITH_JR] == (None, 0.97, date(2026, 10, 30), "@DAL")  # none listed
    assert report.as_of == PLAYERS_AS_OF and report.warnings == []


def test_injury_report_after_his_last_game_still_gives_the_status_chance(week, monkeypatch):
    # Sunday: nobody here plays again this week, but the status still says
    # how likely he is to play his next game, whenever it is.
    monkeypatch.setitem(PLAYERS[MURRAY], "injury_status", "Doubtful")
    week.at(datetime(2026, 11, 1, 12, 0))
    report = service.injury_report(service.load_team(), week.now)
    murray = next(r for r in report.rows if r.player_id == MURRAY)
    assert (murray.next_game, murray.opponent, murray.chance) == (None, None, 0.15)


# ---------- Review ----------

def test_review_grades_fridays_calls_on_each_players_next_game(week):
    team, advice = friday_advice(week)
    service.save_advice(advice)
    week.at(datetime(2026, 11, 2, 9, 0))   # Monday morning: week 2 is over
    entries = service.saved_advice()
    result = service.review(team, entries, date(2026, 11, 2))
    verdicts = {(c.kind, c.player_id): (c.verdict, [g.points for g in c.later])
                for c in result.calls}
    assert verdicts == {
        ("LOCK", CLINGAN): ("RIGHT", [11.4]),          # Saturday's 11.4 < 22.8
        ("PASS", MURRAY): ("RIGHT", [34.4]),           # Friday's 34.4 beat 23.4
        ("PASS", SMITH_JR): ("RIGHT", [16.2, 31.7]),   # Friday's 16.2 beat 14.4
    }
    assert result.tally == {"LOCK": {"RIGHT": 1}, "PASS": {"RIGHT": 2}}
    assert result.calls_in_run(entries[0]["id"]) == result.calls
    assert result.lineup_warnings == [] and result.log_warnings == []


def test_saved_advice_is_empty_before_any_advise_run(week):
    assert service.saved_advice() == [] and service.saved_advice(all_weeks=True) == []
