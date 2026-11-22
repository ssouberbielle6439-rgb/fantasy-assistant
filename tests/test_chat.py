"""The AI chat (chat.py) with Gemini replaced by scripted replies, so no
network: the tool-calling loop, name matching, the number check, and what
happens when something fails.

The tools run on the real engine (service.py) over the frozen week 2 from
test_service.py: Friday 10/30 at noon, Mobley locked in F at 27.9.
"""

import copy
from datetime import datetime

import pytest
import requests

from fantasy_assistant import __main__ as cli
from fantasy_assistant import advice_log, chat, gemini, service
from fantasy_assistant.lineup import LockMove
from test_service import (CLINGAN, FRIDAY_NOON, MOBLEY, MURRAY, PLAYERS, SMITH_JR,  # noqa: F401
                          FrozenWeek, save_mobley_lock, week)

SETTINGS = gemini.Settings("AIza-test-key-not-real", "gemini-3.5-flash-lite")


# ---------- A scripted Gemini ----------

def call(name, call_id=None, signature=None, **args):
    """A functionCall part, as Gemini sends it."""
    part = {"functionCall": {"name": name, "args": args}}
    if call_id:
        part["functionCall"]["id"] = call_id
    if signature:
        part["thoughtSignature"] = signature
    return part


def reply(*parts):
    return {"role": "model", "parts": list(parts)}


def text(t):
    return reply({"text": t})


class ScriptedGemini:
    """Stands in for gemini.generate. Each step is a reply, an exception to
    raise, or a function of the conversation so far (to answer with numbers
    from a tool result). Every request's conversation is recorded."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def __call__(self, settings, contents, system, tools):
        self.requests.append(copy.deepcopy(contents))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return copy.deepcopy(step(contents) if callable(step) else step)


@pytest.fixture
def gemini_says(monkeypatch):
    def install(*steps):
        fake = ScriptedGemini(*steps)
        monkeypatch.setattr(gemini, "generate", fake)
        return fake
    return install


def last_tool_results(contents):
    """{tool name: result} from the conversation's latest message, which holds
    our function responses."""
    return {p["functionResponse"]["name"]: p["functionResponse"]["response"]
            for p in contents[-1]["parts"]}


@pytest.fixture
def friday(week):
    """A chat session at Friday noon of week 2, Mobley locked in F."""
    save_mobley_lock()
    return chat.ChatSession(service.load_team(), SETTINGS, clock=lambda: FRIDAY_NOON)


# ---------- The tool-calling loop ----------

def test_lock_or_wait_runs_lock_decision_and_explains_its_numbers(friday, gemini_says):
    def explain(contents):
        r = last_tool_results(contents)["lock_decision"]
        return text(f"{r['verdict']} Donovan Clingan in {r['slot']}. Locking his {r['score']} "
                    f"adds about {r['gain']} expected points vs. passing.")
    fake = gemini_says(reply(call("lock_decision", "c1", "sig-A", player="Clingan", score=22.8)),
                       explain)
    answer = friday.ask("Clingan scored 22.8, lock or wait?")
    assert answer.text.startswith("LOCK Donovan Clingan in C. Locking his 22.8 adds about")
    assert answer.tools_used == ["lock_decision"] and answer.unmatched == []

    # The second request carries Gemini's call back unchanged (signature and
    # all), then our result, answered to the call's id, in a user message.
    _, model_turn, results = fake.requests[1]
    assert model_turn == reply(call("lock_decision", "c1", "sig-A", player="Clingan", score=22.8))
    [part] = results["parts"]
    assert results["role"] == "user"
    assert (part["functionResponse"]["id"], part["functionResponse"]["name"]) == (
        "c1", "lock_decision")
    response = part["functionResponse"]["response"]
    assert (response["player"], response["verdict"], response["slot"]) == (
        "Donovan Clingan", "LOCK", "C")
    assert response["sleeper_steps"] == "Lock D. Clingan in C (22.8)."
    assert "worth about 18.8 on their own" in response["reason"]


def test_two_tool_calls_in_one_reply_are_answered_together(friday, gemini_says):
    fake = gemini_says(reply(call("get_lineup", "a"), call("get_week_grid", "b")),
                       text("Your lineup is set."))
    answer = friday.ask("What does my week look like?")
    assert answer.tools_used == ["get_lineup", "get_week_grid"]
    results = fake.requests[1][-1]["parts"]
    assert [p["functionResponse"]["id"] for p in results] == ["a", "b"]
    lineup = results[0]["functionResponse"]["response"]
    assert [(s["slot"], s["player"], s["score"], s["locked"]) for s in lineup["slots"]] == [
        ("PG", "Jamal Murray", 23.4, False), ("F", "Evan Mobley", 27.9, True),
        ("C", "Donovan Clingan", 22.8, False), ("UTIL", "Jabari Smith Jr.", 14.4, False)]
    grid = results[1]["functionResponse"]["response"]
    smith = next(p for p in grid["players"] if p["player"] == "Jabari Smith Jr.")
    assert (smith["games_left"], smith["note"]) == (2, "plays today, lock before tip-off")


def test_who_to_start_today_comes_from_advice_which_is_never_saved(friday, gemini_says,
                                                                    monkeypatch):
    lineups = []
    real = service.current_lineup
    monkeypatch.setattr(service, "current_lineup",
                        lambda team, now: lineups.append(now) or real(team, now))
    fake = gemini_says(reply(call("get_lineup"), call("get_advice")),
                       text("Start Jamal Murray at PG tonight @PHX."))
    friday.ask("Who should I start at PG today?")
    advice = last_tool_results(fake.requests[1])["get_advice"]
    assert [(t["slot"], t["start"], t["opponent"]) for t in advice["today"]] == [
        ("PG", "Jamal Murray", "@PHX"), ("UTIL", "Jabari Smith Jr.", "@DAL")]
    assert [(lk["player"], lk["slot"], lk["score"]) for lk in advice["recommended_locks"]] == [
        ("Donovan Clingan", "C", 22.8)]
    assert advice_log.load() == []      # asking the chat never adds to the advice log
    assert len(lineups) == 1            # the lineup is worked out once per question


def test_review_tool_reports_the_tally_with_percent_right(week, friday, gemini_says):
    service.save_advice(service.advise(friday.team, _lineup_with_locks(friday), FRIDAY_NOON))
    week.at(datetime(2026, 11, 2, 9, 0))   # Monday morning: week 2 is over
    friday.clock = lambda: week.now
    fake = gemini_says(reply(call("review")), text("3 of 3 calls were right."))
    friday.ask("How did the app's advice do this week?")
    result = last_tool_results(fake.requests[1])["review"]
    assert result["overall"] == {"right": 3, "graded": 3, "percent_right": 100}
    assert result["tally"]["PASS"]["right"] == 2
    assert {(c["kind"], c["player"], c["verdict"]) for c in result["calls"]} == {
        ("LOCK", "D. Clingan", "RIGHT"), ("PASS", "J. Murray", "RIGHT"),
        ("PASS", "J. Smith Jr.", "RIGHT")}


def test_review_with_nothing_saved_says_so(friday, gemini_says):
    fake = gemini_says(reply(call("review")), text("No advice saved yet."))
    friday.ask("How did the advice do?")
    assert "No advice saved yet" in last_tool_results(fake.requests[1])["review"]["error"]


def test_too_many_tool_rounds_gives_up_and_forgets_the_question(friday, gemini_says):
    fake = gemini_says(*[reply(call("get_week_grid"))] * (chat.MAX_TOOL_ROUNDS + 1))
    answer = friday.ask("Loop forever")
    assert "couldn't work that out" in answer.text
    assert len(fake.requests) == chat.MAX_TOOL_ROUNDS + 1
    assert friday.history == []


def test_a_gemini_failure_mid_question_leaves_the_conversation_clean(friday, gemini_says):
    gemini_says(reply(call("get_lineup")), gemini.RateLimited("limit", 30))
    with pytest.raises(gemini.RateLimited):
        friday.ask("What's my lineup?")
    assert friday.history == []   # no dangling tool call to confuse the next question
    fake = gemini_says(text("Hello."))
    assert friday.ask("Hi").text == "Hello."
    assert fake.requests[0] == [{"role": "user", "parts": [{"text": "Hi"}]}]


def test_a_failing_tool_goes_back_to_gemini_as_an_error_and_to_you(friday, gemini_says,
                                                                    monkeypatch):
    def down(team, now):
        raise requests.ConnectionError("Sleeper is down")
    monkeypatch.setattr(service, "week_grid", down)
    fake = gemini_says(reply(call("get_week_grid")), text("The app couldn't load the week."))
    answer = friday.ask("Who plays Saturday?")
    error = last_tool_results(fake.requests[1])["get_week_grid"]["error"]
    assert error == "get_week_grid failed: ConnectionError: Sleeper is down"
    assert answer.warnings == [error]


def test_made_up_tools_and_arguments_are_errors_not_crashes(friday, gemini_says):
    fake = gemini_says(reply(call("get_news"), call("lock_decision", player="Murray"),
                             call("lock_decision", player="Murray", score="a lot")),
                       text("Sorry."))
    friday.ask("News?")
    errors = [p["functionResponse"]["response"]["error"] for p in fake.requests[1][-1]["parts"]]
    assert errors[0] == "There's no tool named 'get_news'."
    assert errors[1].startswith("Wrong arguments for lock_decision")
    assert errors[2] == "The score has to be a number, not 'a lot'."


# ---------- Injuries ----------

def test_who_is_injured_runs_get_injuries(friday, gemini_says, monkeypatch):
    # Santiago, 11/22: "Which of my players are injured today?" got "The app
    # can't answer that", though the app reads Sleeper's statuses.
    assert "get_injuries" in [t["name"] for t in chat.TOOLS]
    assert "get_injuries" in chat.system_prompt(FRIDAY_NOON)
    monkeypatch.setitem(PLAYERS[MURRAY], "injury_status", "Questionable")
    fake = gemini_says(reply(call("get_injuries")),
                       text("Jamal Murray is Questionable, 65% to play tonight @PHX."))
    answer = friday.ask("Which of my players are injured today?")
    result = last_tool_results(fake.requests[1])["get_injuries"]
    rows = {r["player"]: r for r in result["players"]}
    assert rows["Jamal Murray"] == {"player": "Jamal Murray", "injury_status": "Questionable",
                                    "on_ir": False, "chance_to_play_next_game_pct": 65,
                                    "next_game": "Fri 10/30 @PHX"}
    assert rows["Donovan Clingan"]["injury_status"] == "none listed"
    assert rows["Donovan Clingan"]["chance_to_play_next_game_pct"] == 97
    assert result["statuses_as_of"] == "Fri 10/30 8:15am"
    assert answer.unmatched == [] and answer.tools_used == ["get_injuries"]


# ---------- Lock labels (target and floor) ----------

def test_should_i_lock_anyone_gets_the_below_target_label_when_the_answer_drops_it(
        friday, gemini_says):
    # Santiago, 11/22: "Should I lock anyone tonight?" said lock Brooks at 23.2
    # without "below your 25 target", which advise shows. Same here with
    # Clingan's 22.8: the tool result carries the label, and if the answer
    # leaves it out the app adds it.
    fake = gemini_says(reply(call("get_advice")), text("Yes: lock Donovan Clingan in C."))
    answer = friday.ask("Should I lock anyone tonight?")
    [lock] = last_tool_results(fake.requests[1])["get_advice"]["recommended_locks"]
    assert (lock["player"], lock["score"], lock["label"]) == (
        "Donovan Clingan", 22.8, "below your 25 target")
    assert answer.labels == ["D. Clingan's 22.8 in C is below your 25 target"]


def test_an_answer_that_gives_the_label_needs_no_label_check(friday, gemini_says):
    gemini_says(reply(call("get_advice")),
                text("LOCK Donovan Clingan in C (below your 25 target)."))
    assert friday.ask("Should I lock anyone tonight?").labels == []


def test_an_answer_about_someone_else_gets_no_lock_label(friday, gemini_says):
    gemini_says(reply(call("get_advice")), text("Start Jamal Murray at PG tonight @PHX."))
    assert friday.ask("Who starts at PG today?").labels == []


def test_lock_decision_answer_gets_the_label_too(friday, gemini_says):
    fake = gemini_says(reply(call("lock_decision", player="Clingan", score=22.8)),
                       text("LOCK Donovan Clingan in C."))
    answer = friday.ask("Clingan scored 22.8, lock or wait?")
    assert last_tool_results(fake.requests[1])["lock_decision"]["label"] == "below your 25 target"
    assert answer.labels == ["D. Clingan's 22.8 in C is below your 25 target"]


def test_labels_are_checked_per_question(friday, gemini_says):
    gemini_says(reply(call("get_advice")), text("Lock Clingan (below your 25 target)."),
                text("Clingan plays Saturday."))
    friday.ask("Should I lock anyone?")
    assert friday.ask("When does Clingan play next?").labels == []   # no lock talk this time


def test_held_lock_answer_gets_the_floor_label(friday, gemini_says, monkeypatch):
    held = LockMove(SMITH_JR, "UTIL", "UTIL", 21.0, 1.5)
    monkeypatch.setattr(service, "_advise", lambda team, lineup, now, what_if=None:
                        service.Advice([], held=[held], floor=23.0, values={SMITH_JR: 19.6}))
    gemini_says(reply(call("lock_decision", player="Smith Jr.", score=21)),
                text("HELD: Jabari Smith Jr.'s 21.0 is your call."))
    answer = friday.ask("Smith Jr. scored 21, lock?")
    assert answer.labels == ["J. Smith Jr.'s 21.0 in UTIL is held back by today's 23-point floor"]


def test_the_rules_ask_for_the_label_with_the_call():
    rules = chat.system_prompt(FRIDAY_NOON)
    assert "(below your 25 target)" in rules and "held back by the floor" in rules


def test_paul_george_in_an_answer_doesnt_trigger_kyshawn_georges_label(gemini_says):
    # Live run 11/22: K. George's lock was held by the floor, the answer named
    # only Paul George, and the label check matched "George" to the wrong one.
    session = chat.ChatSession(TEAM, SETTINGS, clock=lambda: FRIDAY_NOON)
    kyshawn = chat.resolve_player(TEAM, "Kyshawn George").player_id
    session._note_label(kyshawn, 19.6, "SF", "held back by today's 20-point floor")
    assert session.missing_labels("LOCK Paul George in SF (29.5).") == []
    assert session.missing_labels("Kyshawn George's 19.6 is your call.") == [
        "K. George's 19.6 in SF is held back by today's 20-point floor"]
    assert session.missing_labels("K. George's 19.6 is your call.") != []
    assert session.missing_labels("K. George: held back by the floor.") == []


@pytest.mark.parametrize("text, kyshawn, paul, murray", [
    ("LOCK Paul George in SF.", False, True, False),
    ("Kyshawn George and Paul George both play.", True, True, False),
    ("K. George at SF, Murray at PG.", True, False, True),   # Murray: the only Murray
    ("George plays tonight.", False, False, False),            # which George? neither
])
def test_mentions_tells_players_with_the_same_last_name_apart(text, kyshawn, paul, murray):
    pid = {name: chat.resolve_player(TEAM, name).player_id
           for name in ("Kyshawn George", "Paul George", "Jamal Murray")}
    assert (chat.mentions(TEAM, pid["Kyshawn George"], text),
            chat.mentions(TEAM, pid["Paul George"], text),
            chat.mentions(TEAM, pid["Jamal Murray"], text)) == (kyshawn, paul, murray)


# ---------- Player names ----------

def roster_team(*names):
    """A made-up team with these players (ids '1', '2', ...)."""
    players = {}
    for i, full in enumerate(names, 1):
        first, last = full.split(" ", 1)
        players[str(i)] = {"first_name": first, "last_name": last, "full_name": full,
                           "team": "DEN", "fantasy_positions": ["SF"]}
    return service.Team({"user_id": "u"}, {"scoring_settings": {}},
                        {"players": list(players), "reserve": []}, players)


TEAM = roster_team("Jabari Smith Jr.", "Jalen Smith", "Kyshawn George", "Paul George",
                   "Jamal Murray", "Nikola Jokić", "Kelly Oubre Jr.")


@pytest.mark.parametrize("typed, expected", [
    ("Smith Jr.", "Jabari Smith Jr."),     # the suffix tells the Smiths apart
    ("smith jr", "Jabari Smith Jr."),
    ("Jalen Smith", "Jalen Smith"),        # an exact full name wins
    ("J. Murray", "Jamal Murray"),         # the app's own short form
    ("Murray", "Jamal Murray"),
    ("jamal", "Jamal Murray"),
    ("Jokic", "Nikola Jokić"),             # accents don't matter
    ("Kyshawn", "Kyshawn George"),
    ("K. George", "Kyshawn George"),
    ("Oubre", "Kelly Oubre Jr."),          # no suffix typed: still him
])
def test_names_resolve_to_one_roster_player(typed, expected):
    match = chat.resolve_player(TEAM, typed)
    assert TEAM.name(match.player_id) == expected


@pytest.mark.parametrize("typed, could_be", [
    ("Smith", ["Jabari Smith Jr.", "Jalen Smith"]),
    ("George", ["Kyshawn George", "Paul George"]),
    ("J. Smith", ["Jabari Smith Jr.", "Jalen Smith"]),
])
def test_ambiguous_names_list_every_match(typed, could_be):
    match = chat.resolve_player(TEAM, typed)
    assert match.ambiguous and [TEAM.name(p) for p in match.candidates] == could_be


def test_a_name_not_on_the_roster_matches_nobody():
    match = chat.resolve_player(TEAM, "LeBron James")
    assert (match.player_id, match.candidates) == (None, [])


def test_ambiguous_name_asks_you_then_carries_on_with_your_answer(gemini_says, monkeypatch):
    asked = []
    monkeypatch.setattr(service, "player_profile", lambda team, pid: asked.append(pid) or
                        service.ProfileView(pid, team.name(pid), None, []))
    session = chat.ChatSession(TEAM, SETTINGS, clock=lambda: FRIDAY_NOON)

    # Gemini passes "George" on; the app asks you itself, without asking
    # Gemini again (so it can't guess).
    fake = gemini_says(reply(call("player_profile", "p1", player="George")))
    answer = session.ask("How good is George?")
    assert answer.asking
    assert answer.text == 'Which "George" do you mean: Kyshawn George or Paul George?'
    assert len(fake.requests) == 1 and asked == []
    assert [m["role"] for m in session.history] == ["user", "model", "user", "model"]
    result = session.history[2]["parts"][0]["functionResponse"]["response"]
    assert "could be Kyshawn George, Paul George" in result["error"]
    assert "_ambiguous" not in result   # internal marker, never sent to Gemini

    # Your answer goes to Gemini with the question it answers.
    fake = gemini_says(reply(call("player_profile", "p2", player="Paul George")),
                       text("Not enough games yet for Paul George."))
    session.ask("Paul")
    assert fake.requests[0][-2:] == [
        {"role": "model", "parts": [{"text": answer.text}]},
        {"role": "user", "parts": [{"text": "Paul"}]}]
    assert [TEAM.name(p) for p in asked] == ["Paul George"]


def test_a_player_not_on_the_roster_gets_the_roster_back(friday, gemini_says):
    fake = gemini_says(reply(call("player_profile", player="LeBron James")),
                       text("LeBron James isn't on your roster."))
    friday.ask("How's LeBron doing?")
    result = last_tool_results(fake.requests[1])["player_profile"]
    assert result["error"] == "No one on the manager's roster matches 'LeBron James'."
    assert "Jamal Murray" in result["roster"]


def test_smith_jr_scored_26_runs_on_jabari(friday, gemini_says):
    fake = gemini_says(reply(call("lock_decision", player="Smith Jr.", score=26)), text("ok"))
    friday.ask("Smith Jr. scored 26, lock or wait?")
    result = last_tool_results(fake.requests[1])["lock_decision"]
    assert (result["player"], result["score"], result["slot"]) == ("Jabari Smith Jr.", 26.0,
                                                                   "UTIL")


# ---------- The number check ----------

def test_number_check_flags_a_decimal_no_tool_gave(friday, gemini_says):
    gemini_says(reply(call("lock_decision", player="Clingan", score=22.8)),
                text("LOCK Donovan Clingan in C: his 22.8 beats Saturday by 3.4 points."))
    answer = friday.ask("Clingan scored 22.8, lock?")
    assert answer.unmatched == ["3.4"]   # the AI's own math, not a tool's number


def test_number_check_accepts_numbers_you_typed_and_ignores_counts_and_slots(friday,
                                                                              gemini_says):
    gemini_says(text("You said 26.0 for UTIL2; he has 2 games left on 10/31."))
    assert friday.ask("He scored 26").unmatched == []


def test_numbers_are_found_inside_tool_text_too():
    found = chat.numbers_in({"reason": "adds about 4.1 (worth about 18.8)", "gain": -1.25,
                             "locked": True, "games": [{"pct": 97}]})
    assert found == {4.1, 18.8, 1.2, 97.0}
    assert chat.unmatched_numbers("Lock at 18.8, gain 4.1, not 5.5 or 5.5.", found) == ["5.5"]


def test_the_rules_ban_the_ais_own_arithmetic():
    rules = chat.system_prompt(FRIDAY_NOON)
    assert "Don't do arithmetic" in rules and "lock_decision's gain" in rules
    assert "Friday 10/30/2026, 12:00pm" in rules


def test_reply_text_skips_the_models_thinking():
    content = reply({"text": "plan: call tools", "thought": True}, {"text": "LOCK him."})
    assert chat.reply_text(content) == "LOCK him."


# ---------- The command ----------

def run_chat(monkeypatch, capsys, *typed):
    lines = iter(typed)

    def fake_input(prompt):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None
    monkeypatch.setattr("builtins.input", fake_input)
    code = cli.cmd_chat()
    return code, capsys.readouterr().out


def test_chat_loads_the_team_once_for_the_whole_session(week, gemini_says, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SETTINGS.key)
    loads = []
    real = service.load_team
    monkeypatch.setattr(service, "load_team", lambda: loads.append(1) or real())
    fake = gemini_says(text("One."), text("Two."))
    code, out = run_chat(monkeypatch, capsys, "First question", "Second question")
    assert code == 0 and len(loads) == 1 and len(fake.requests) == 2
    assert "App > One." in out and "App > Two." in out
    assert SETTINGS.key not in out


def test_chat_without_a_key_stops_before_loading_anything(week, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(service, "load_team", lambda: pytest.fail("loaded the team"))
    with pytest.raises(SystemExit, match="Set GEMINI_API_KEY"):
        cli.cmd_chat()


def test_a_bad_key_ends_the_chat_but_a_rate_limit_doesnt(week, gemini_says, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SETTINGS.key)
    gemini_says(gemini.RateLimited("Gemini's free-tier limit is used up for now."),
                text("Answered."), gemini.BadKey("Gemini rejected the key."))
    code, out = run_chat(monkeypatch, capsys, "one", "two", "three", "never asked")
    assert code == 1
    assert out.index("(Gemini's free-tier limit") < out.index("App > Answered.") < out.index(
        "(Gemini rejected the key.)")


def test_chat_prints_the_number_check_and_the_tools_used(week, gemini_says, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SETTINGS.key)
    save_mobley_lock()
    monkeypatch.setattr(chat, "datetime", type("Clock", (), {"now": staticmethod(
        lambda: FRIDAY_NOON)}))
    gemini_says(reply(call("lock_decision", player="Clingan", score=22.8)),
                text("LOCK Donovan Clingan in C, 9.9 better than waiting."))
    _, out = run_chat(monkeypatch, capsys, "Clingan 22.8?")
    assert "(Number check: 9.9 does not match any number the app gave." in out
    assert "(Label check: D. Clingan's 22.8 in C is below your 25 target.)" in out
    assert "(from: lock_decision)" in out


def _lineup_with_locks(session):
    lineup = service.current_lineup(session.team, FRIDAY_NOON)
    service.keep_saved_locks(lineup)
    return lineup
