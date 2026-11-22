"""The AI chat: ask about your lineup in plain English.

    "Smith Jr. scored 26, lock or wait?"
    "Who should I start at G today?"
    "How did the app's advice do this week?"

How one question works (ChatSession.ask):

  1. The question goes to Gemini with the rules (system_prompt) and the list
     of tools it may call (TOOLS).
  2. Gemini either answers in text or asks for tools: "call lock_decision
     with player='Smith Jr.', score=26".
  3. We run each tool on service.py, the same engine the command line uses,
     and send the results back. Repeat until Gemini answers in text (at most
     MAX_TOOL_ROUNDS rounds).

The math decides; the AI only explains. Every verdict and number comes from
a tool. The system prompt says so, and two checks in code back it up: the
number check (unmatched_numbers) flags any decimal in an answer that no tool
gave and the user didn't type, and the label check (ChatSession.missing_labels)
adds advise's "below your 25 target" or "held back by today's floor" to a lock
answer that left it out.

Names: tools take player names as the user wrote them, and resolve_player
matches them to the roster. When a name could be two players ("George"),
the app asks the user itself, without letting Gemini guess.

Nothing here saves to the advice log: get_advice calls service.advise but
never save_advice, so asking the chat doesn't add runs for `review` to grade.
"""

from __future__ import annotations

import inspect
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from . import gemini, service
from .advice_log import FREE, PENDING, RIGHT, WRONG
from .lineup import today_line

MAX_TOOL_ROUNDS = 5   # tool rounds per question before giving up (stops a runaway loop)


# ---------- Player names ----------

def name_words(name: str) -> list[str]:
    """'J. Smith Jr.' -> ['j', 'smith', 'jr']; 'Nikola Jokić' -> ['nikola', 'jokic'].
    Accents, case, dots and apostrophes don't matter; suffixes are kept, so
    'Smith Jr.' can tell Jabari Smith Jr. from Jalen Smith."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = ascii_name.lower().replace(".", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", " ", ascii_name).split()


@dataclass
class NameMatch:
    """Who a typed name means: one roster player, several (ambiguous), or nobody."""
    query: str
    player_id: str | None = None
    candidates: list[str] = field(default_factory=list)   # player ids, when ambiguous

    @property
    def ambiguous(self) -> bool:
        return self.player_id is None and len(self.candidates) > 1


def resolve_player(team: service.Team, name: str) -> NameMatch:
    """Match a typed name to the roster.

    1. Exact full name ("Jalen Smith"), so a full name always wins.
    2. Otherwise every typed word must start a word of his name, in any
       order: "Murray", "J. Murray", "jamal", "Smith Jr." all work, and
       "Smith" matches both Smiths, which makes it ambiguous.
    """
    words = name_words(name)
    if not words:
        return NameMatch(name)
    full = {pid: name_words(team.name(pid)) for pid in team.player_ids}
    exact = [pid for pid, w in full.items() if w == words]
    if len(exact) == 1:
        return NameMatch(name, exact[0])
    partial = [pid for pid, w in full.items()
               if all(any(part.startswith(q) for part in w) for q in words)]
    if len(partial) == 1:
        return NameMatch(name, partial[0])
    return NameMatch(name, None, exact or partial)


def which_player_question(team: service.Team, match: NameMatch) -> str:
    """'Which "George" do you mean: Kyshawn George or Paul George?'"""
    names = [team.name(pid) for pid in match.candidates]
    options = ", ".join(names[:-1]) + f" or {names[-1]}"
    return f'Which "{match.query}" do you mean: {options}?'


# ---------- The number check ----------

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DECIMAL = re.compile(r"\d+\.\d+")


def numbers_in(value) -> set[float]:
    """Every number in a tool result or a message (inside text too), rounded
    to one decimal, sign dropped. These are the numbers an answer may use."""
    found: set[float] = set()
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, (int, float)):
        found.add(round(abs(float(value)), 1))
    elif isinstance(value, str):
        found.update(round(float(n), 1) for n in _NUMBER.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= numbers_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found |= numbers_in(v)
    return found


def unmatched_numbers(text: str, known: set[float]) -> list[str]:
    """Decimals in an answer that no tool gave and the user didn't type.

    Only numbers with a decimal point are checked: fantasy scores, gains and
    expected points always have one, while counts, dates and slot names
    (UTIL2, 3 games, 11/20) don't. The system prompt bans the AI's own
    arithmetic, so a flagged number is a made-up one or the AI's own math.
    """
    flagged = []
    for n in _DECIMAL.findall(text):
        if round(float(n), 1) not in known and n not in flagged:
            flagged.append(n)
    return flagged


# ---------- What Gemini is told ----------

def system_prompt(now: datetime) -> str:
    """The rules for Gemini. Sent with every request, with the current time
    (it decides what "today" and "tonight" mean)."""
    clock = f"{now.hour % 12 or 12}:{now:%M}{'am' if now.hour < 12 else 'pm'}"
    return f"""\
You are the chat inside a fantasy basketball assistant for one Sleeper league \
in Lock-In Mode. You are talking to the team's manager. It is {now:%A %m/%d/%Y}, {clock}.

How Lock-In Mode works: only one game per player counts each week, and each \
starting slot ends the week with one game. After a player's game the manager \
can lock that score in his slot (then that slot and that player are done for \
the week) or pass, and his next game replaces the score. Weeks run Monday to Sunday.

Rules. Follow all of them:
1. The app's math decides; you only explain it. Use the tools for every fact \
about the manager's team, players, scores, schedule and advice. Repeat the \
tools' verdicts (LOCK, PASS, HELD, ...) and slots without changing them.
2. Every number you write must appear in a tool result or in the manager's message.
3. Don't do arithmetic. No differences, totals, averages, percentages or \
comparisons that you work out yourself. If you want to say how much one option \
beats another, use a number a tool gives for it (for example lock_decision's \
gain, or a slot's expected points). If no tool gives that number, say it without one.
4. If no tool answers the question, say the app can't answer that. Don't guess, \
and don't use outside knowledge about players, injuries, news or other teams.
5. Be short: the call first (for example "LOCK Jabari Smith Jr. in F." or \
"Start Jamal Murray at G."), then the reason in one or two sentences. No lists, \
tables or headings unless the manager asks for them.
6. Pass player names to the tools as the manager wrote them. The app matches \
them to the roster and asks the manager if a name is unclear. If a tool says a \
name isn't on the roster, say so.
7. For a score the manager gives you ("he scored 26"), use lock_decision. For \
the scores already in the lineup, and for who to start today, use get_advice. \
For injuries and who might sit, use get_injuries (Sleeper's injury statuses).
8. If a tool result has warnings that change the answer, mention them in a few words.
9. When a lock comes with a label (below the manager's target, or held back by \
today's floor), put the label with the call, for example "LOCK Jamal Murray in \
PG (below your 25 target)." When the manager asks what to lock, also mention \
locks held back by the floor, as his call.

What the app's words mean: LOCK = lock now. PASS = keep the slot open for a \
better game. HELD = the math would lock it, but it's under today's floor, so \
it's the manager's call. WAIT = the player's game hasn't happened yet. \
PLAYING = his game is on right now and replaces the slot's score when it ends. \
OPEN = empty slot. Gain = expected points a lock adds to the week compared with \
passing. Expected = what the app expects that slot to end up locking this week. \
The target is the manager's preferred lock score; the floor is the lowest score \
worth locking today (lower on Sunday). A free roll = starting a spare who plays \
today in a slot whose planned player doesn't."""


_PLAYER = {"type": "string",
           "description": "The player's name as the manager wrote it (full name, last name, "
                          "or 'J. Murray'). The app matches it to the manager's roster."}

TOOLS = [
    {"name": "get_lineup",
     "description": "The manager's starting lineup right now: each slot's player, current "
                    "score, and whether it's locked. Locks are the ones saved by the advise "
                    "command."},
    {"name": "get_advice",
     "description": "The app's advice for the lineup right now: which scores in hand to lock "
                    "and in which slot (each with its gain in expected points and the Sleeper "
                    "steps), locks held back by today's floor, each open slot's status and the "
                    "player it waits on with expected points, the weakest open slots, and "
                    "today's plan: who to start in each open slot today and how to move him "
                    "there. Use for 'what should I lock', 'who should I start at G today'."},
    {"name": "lock_decision",
     "description": "Lock or wait for one player at a score the manager gives ('Smith Jr. "
                    "scored 26, lock or wait?'). Runs the whole-lineup math as if his latest "
                    "game scored that and is over. Returns the verdict (LOCK, HELD, PASS, "
                    "ALREADY_LOCKED, NOT_STARTING or WEEK_DONE), the slot, the gain in "
                    "expected points vs. passing, what his own remaining games are worth, "
                    "his games left, and a one-sentence reason with the numbers.",
     "parameters": {"type": "object",
                    "properties": {"player": _PLAYER,
                                   "score": {"type": "number",
                                             "description": "The fantasy points he scored."}},
                    "required": ["player", "score"]}},
    {"name": "player_profile",
     "description": "One roster player's numbers: average fantasy points per game, swing "
                    "(standard deviation), games used, chance to play, injury status, and his "
                    "lock bars: lock a score at least this high with 1, 2 or 3 games left.",
     "parameters": {"type": "object", "properties": {"player": _PLAYER},
                    "required": ["player"]}},
    {"name": "get_injuries",
     "description": "Every roster player's injury status from Sleeper (Out, Questionable, "
                    "Doubtful, ... or none listed), whether he's on IR, his next game this "
                    "week, and his chance to play that next game. Use for 'who's injured', "
                    "'is X playing tonight', 'who might sit'."},
    {"name": "get_week_grid",
     "description": "Who on the roster plays which day this fantasy week (Monday to Sunday): "
                    "each player's games, games left, chance to play his next game, whether "
                    "his game is on now, and how many players play each day."},
    {"name": "review",
     "description": "How the app's saved advice did: each LOCK, PASS and HELD call graded "
                    "RIGHT, WRONG, PENDING or FREE on the player's next game, the tally per "
                    "kind with percent right, how the manager's overrides did, and the "
                    "forecast check. This week by default.",
     "parameters": {"type": "object",
                    "properties": {"all_weeks": {"type": "boolean",
                                                 "description": "true for every week of the "
                                                                "season, not just this one."}}}},
]


# ---------- The chat session ----------

@dataclass
class Answer:
    text: str
    tools_used: list[str]                 # in the order Gemini called them
    unmatched: list[str] = field(default_factory=list)  # decimals the number check couldn't find
    warnings: list[str] = field(default_factory=list)   # data problems not shown before
    asking: bool = False                  # the app is asking which player the user meant
    labels: list[str] = field(default_factory=list)  # lock labels the answer left out


class ChatSession:
    """One conversation. The team is loaded once (by the caller) and kept for
    the whole session; the lineup and advice are worked out fresh for each
    question, since games finish while you chat, but only once per question."""

    def __init__(self, team: service.Team, settings: gemini.Settings,
                 clock: Callable[[], datetime] | None = None):
        # The clock is looked up here, not as a default argument (fixed at
        # import), so tests can swap it. Each question reads it again.
        self.team, self.settings, self.clock = team, settings, clock or datetime.now
        self.history: list[dict] = []        # the conversation, as Gemini's contents
        self.known_numbers: set[float] = set()   # numbers from tool results and the user
        self._seen_warnings: set[str] = set()
        self._cache: dict = {}
        self._labels: list[tuple[str, str, str]] = []   # this question's (player, label line, keyword)
        self.now = self.clock()

    def ask(self, question: str) -> Answer:
        """Answer one question. Raises gemini.GeminiError if Gemini fails; the
        question is then forgotten, so the conversation stays valid."""
        start = len(self.history)
        self.now, self._cache, self._labels = self.clock(), {}, []
        self.known_numbers |= numbers_in(question)
        self.history.append({"role": "user", "parts": [{"text": question}]})
        used: list[str] = []
        warnings: list[str] = []
        try:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                content = gemini.generate(self.settings, self.history,
                                          system_prompt(self.now), TOOLS)
                self.history.append(content)   # as sent: keeps any thoughtSignature
                calls = [p["functionCall"] for p in content["parts"] if "functionCall" in p]
                if not calls:
                    text = reply_text(content)
                    if not text:
                        raise gemini.BadReply("Gemini sent back an empty answer. "
                                              "Try rewording the question.")
                    return Answer(text, used, unmatched_numbers(text, self.known_numbers),
                                  warnings, labels=self.missing_labels(text))
                if round_no == MAX_TOOL_ROUNDS:
                    break
                question_back = self._run_calls(calls, used, warnings)
                if question_back:
                    self.history.append({"role": "model", "parts": [{"text": question_back}]})
                    return Answer(question_back, used, [], warnings, asking=True)
        except gemini.GeminiError:
            del self.history[start:]
            raise
        del self.history[start:]
        return Answer("Sorry, I couldn't work that out (too many steps). Try asking about one "
                      "thing at a time.", used, [], warnings)

    def _run_calls(self, calls: list[dict], used: list[str], warnings: list[str]) -> str | None:
        """Run the tools Gemini asked for and add their results to the
        conversation (all in one message, matched to each call by its id).
        Returns a question for the user if a player name was ambiguous."""
        parts, questions = [], []
        for call in calls:
            name = call.get("name", "")
            used.append(name)
            result = self._run_tool(name, call.get("args") or {})
            if "_ambiguous" in result:
                questions.append(which_player_question(self.team, result.pop("_ambiguous")))
            for w in result.get("warnings") or []:
                if w not in self._seen_warnings:
                    self._seen_warnings.add(w)
                    warnings.append(w)
            self.known_numbers |= numbers_in(result)
            response = {"name": name, "response": result}
            if call.get("id"):
                response["id"] = call["id"]
            parts.append({"functionResponse": response})
        self.history.append({"role": "user", "parts": parts})
        return "\n".join(questions) or None

    def _run_tool(self, name: str, args: dict) -> dict:
        """One tool's result as a small JSON-ready dict. A tool that fails
        returns {"error": ...}, so Gemini can say what went wrong."""
        tool = {"get_lineup": self.tool_get_lineup, "get_advice": self.tool_get_advice,
                "lock_decision": self.tool_lock_decision,
                "player_profile": self.tool_player_profile,
                "get_injuries": self.tool_get_injuries,
                "get_week_grid": self.tool_get_week_grid, "review": self.tool_review}.get(name)
        if tool is None:
            return {"error": f"There's no tool named {name!r}."}
        try:
            inspect.signature(tool).bind(**args)
        except TypeError as err:   # arguments Gemini made up, or left out
            return {"error": f"Wrong arguments for {name}: {err}"}
        try:
            return tool(**args)
        except Exception as err:   # a data source failed; tell Gemini, and the user
            problem = f"{name} failed: {type(err).__name__}: {err}"
            return {"error": problem, "warnings": [problem]}

    # ----- The label check -----

    def _note_label(self, pid: str, score: float, slot: str, label: str) -> None:
        """Remember a lock label from this question's tool results, so the
        answer can be checked for it."""
        line = f"{self.team.short(pid)}'s {score:.1f} in {slot} is {label}"
        keyword = "floor" if "floor" in label else "target"
        if (pid, line, keyword) not in self._labels:
            self._labels.append((pid, line, keyword))

    def missing_labels(self, text: str) -> list[str]:
        """The lock labels an answer should have shown and didn't.

        A label counts as shown if the answer says "target" (or "floor" for a
        held lock). Only players the answer names are checked (mentions), so
        an answer about someone else doesn't get labels it has nothing to do with.
        """
        lower = text.lower()
        return [line for pid, line, keyword in self._labels
                if keyword not in lower and mentions(self.team, pid, text)]

    # ----- Shared within one question -----

    def _lineup(self) -> service.Lineup:
        if "lineup" not in self._cache:
            lineup = service.current_lineup(self.team, self.now)
            service.keep_saved_locks(lineup)
            self._cache["lineup"] = lineup
        return self._cache["lineup"]

    def _advice(self) -> service.Advice:
        if "advice" not in self._cache:
            self._cache["advice"] = service.advise(self.team, self._lineup(), self.now)
        return self._cache["advice"]

    def _player(self, typed) -> tuple[str | None, dict | None]:
        """(player id, None), or (None, the error result to send back)."""
        match = resolve_player(self.team, str(typed or ""))
        if match.player_id:
            return match.player_id, None
        if match.ambiguous:
            names = [self.team.name(pid) for pid in match.candidates]
            return None, {"error": f"{typed!r} could be {', '.join(names)}. The app is asking "
                                   f"the manager which one.", "_ambiguous": match}
        return None, {"error": f"No one on the manager's roster matches {typed!r}.",
                      "roster": [self.team.name(pid) for pid in self.team.player_ids]}

    # ----- The tools -----

    def tool_get_lineup(self) -> dict:
        lineup = self._lineup()
        return {
            "week": lineup.week_no,
            "days": f"{_day(lineup.days[0])} to {_day(lineup.days[-1])}",
            "slots": [{"slot": s.label, "player": self._name(s.player_id), "score": _r(s.score),
                       "locked": s.locked} for s in lineup.slots],
            "about_locks": "Locked = saved as locked by the advise command. A slot locked in "
                           "Sleeper since then shows as locked once the manager runs advise.",
            "notes": lineup.notes,
            "warnings": lineup.warnings,
        }

    def tool_get_advice(self) -> dict:
        advice = self._advice()   # never saved to the advice log
        warnings = self._lineup().warnings + advice.warnings
        if advice.final_scores:
            return {"week_done": True, "final_scores": advice.final_scores, "warnings": warnings}
        name = self._name
        slots = []
        for r in advice.slots:
            row = {"slot": r.label, "player": name(r.player_id), "score": _r(r.score),
                   "status": r.status}
            if r.expected is not None:   # open slots: the plan for the week
                row.update(waiting_on=name(r.waiting_on), expected=_r(r.expected),
                           depth=r.depth, alternates=[name(p) for p in r.alternates])
            slots.append(row)
        locks = []
        for st in advice.locks:
            m = st.move
            lock = {"player": name(m.player_id), "slot": m.to_label, "from_slot": m.from_label,
                    "score": _r(m.score), "gain": _r(m.gain), "sleeper_steps": st.instruction,
                    "lock_before_tipoff": st.before_tipoff}
            if st.below_target:   # advise marks it "[below your 25 target]"
                lock["label"] = service.target_label(advice.target)
                self._note_label(m.player_id, m.score, m.to_label, lock["label"])
            locks.append(lock)
        held = []
        for h in advice.held:
            label = service.floor_label(advice.floor)
            held.append({"player": name(h.player_id), "slot": h.to_label, "score": _r(h.score),
                         "gain": _r(h.gain), "label": label})
            self._note_label(h.player_id, h.score, h.to_label, label)
        return {
            "week_done": False,
            "lock_target": _r(advice.target),
            "lock_floor_today": _r(advice.floor),
            "recommended_locks": locks,
            "total_gain_of_recommended_locks": _r(advice.total_gain),
            "held_by_floor": held,
            "slots": slots,
            "weakest_open_slots": [{"slot": label, "expected": _r(exp)}
                                   for label, exp in advice.weakest],
            "today": [{"slot": t.move.label, "start": name(t.move.player_id),
                       "opponent": t.opponent,
                       "how": " ".join(today_line(t.move, t.opponent, self.team.short).split())}
                      for t in advice.today],
            "warnings": warnings,
        }

    def tool_lock_decision(self, player, score) -> dict:
        pid, error = self._player(player)
        if error:
            return error
        try:
            points = float(score)
        except (TypeError, ValueError):
            return {"error": f"The score has to be a number, not {score!r}."}
        call = service.lock_decision(self.team, pid, points, self.now)
        if call.label:
            self._note_label(pid, call.score, call.slot, call.label)
        return {
            "player": self.team.name(pid),
            "score": _r(call.score),
            "verdict": call.verdict,
            "slot": call.slot,
            "label": call.label,
            "gain": _r(call.gain),
            "sleeper_steps": call.instruction,
            "his_remaining_games_worth": _r(call.bar),
            "games_left": [{"day": _day(g.day), "opponent": g.opponent,
                            "chance_to_play_pct": round(g.forecast.play_prob * 100)}
                           for g in call.games_left],
            "lock_floor_today": _r(call.floor),
            "lock_target": _r(call.target),
            "reason": call.reason,
            "warnings": call.warnings,
        }

    def tool_player_profile(self, player) -> dict:
        pid, error = self._player(player)
        if error:
            return error
        view = service.player_profile(self.team, pid)
        p = self.team.player(pid)
        result = {"player": view.name, "nba_team": p.get("team"),
                  "positions": "/".join(p.get("fantasy_positions") or [p.get("position") or "?"]),
                  "injury_status": p.get("injury_status") or "none listed",
                  "on_ir": pid in self.team.injured_reserve}
        prof = view.profile
        if view.no_logs:
            result["error"] = f"No game logs for him: {view.no_logs}"
        elif prof is None:
            result["note"] = "Not enough games yet for a profile."
        else:
            result.update(
                games_used=prof.games_used, average=_r(prof.average), swing=_r(prof.swing),
                chance_to_play_pct=round(prof.play_prob * 100),
                lock_bars={f"{n}_game{'s' if n > 1 else ''}_left": _r(bar)
                           for n, bar in zip((1, 2, 3), view.lock_bars)},
                about_lock_bars="For a typical game of his: lock any score at least this high "
                                "when he has this many games left.")
        result["warnings"] = view.warnings
        return result

    def tool_get_injuries(self) -> dict:
        report = service.injury_report(self.team, self.now)
        players = []
        for r in report.rows:
            row = {"player": r.name, "injury_status": r.status or "none listed",
                   "on_ir": r.on_ir, "chance_to_play_next_game_pct": round(r.chance * 100),
                   "next_game": (f"{_day(r.next_game)} {r.opponent}" if r.next_game
                                 else "no games left this week")}
            if r.playing_now:
                row["playing_now"] = True
            players.append(row)
        return {"players": players,
                "statuses_as_of": _when(report.as_of) if report.as_of else "unknown",
                "about": "Injury statuses are Sleeper's, from its player list, which the app "
                         "downloads at most once a day (statuses_as_of says when). Chance to "
                         "play is the app's number for each status, for his next game: Out "
                         "means out for the next game only.",
                "warnings": report.warnings}

    def tool_get_week_grid(self) -> dict:
        grid = service.week_grid(self.team, self.now)
        players = []
        for r in grid.rows:
            row = {"player": r.name, "games": {_day(d): opp for d, opp in r.games.items()},
                   "games_left": r.left, "on_ir": r.on_ir}
            if r.injury_status:
                row["injury_status"] = r.injury_status
            if r.next_chance is not None:
                row["next_game_chance_to_play_pct"] = round(r.next_chance * 100)
            if r.tipoff_note:
                row["note"] = r.tipoff_note
            if r.playing_now:
                row["playing_now"] = True
            players.append(row)
        return {"days": [_day(d) for d in grid.days], "today": _day(grid.today),
                "players": players,
                "players_playing_per_day": {_day(d): n for d, n in grid.playing_per_day.items()
                                            if d >= grid.today},
                "about": "Games left doesn't count a game on right now (it only counts for a "
                         "starter; get_advice knows who's starting).",
                "warnings": grid.warnings}

    def tool_review(self, all_weeks=False) -> dict:
        every_week = all_weeks if isinstance(all_weeks, bool) else (
            str(all_weeks).lower() in ("true", "1", "yes"))
        entries = service.saved_advice(all_weeks=every_week)
        if not entries:
            return {"error": "No advice saved yet. Each run of the advise command is saved, "
                             "and review grades those runs."}
        result = service.review(self.team, entries, self.now.date())
        tally = {}
        right_total = graded_total = 0
        for kind, counts in result.tally.items():
            right, wrong = counts.get(RIGHT, 0), counts.get(WRONG, 0)
            right_total, graded_total = right_total + right, graded_total + right + wrong
            tally[kind] = {"right": right, "wrong": wrong, "graded": right + wrong,
                           "percent_right": round(100 * right / (right + wrong))
                           if right + wrong else None,
                           "pending": counts.get(PENDING, 0), "free": counts.get(FREE, 0)}
        beat, overridden = result.overrides
        gap = result.forecast_gap
        return {
            "weeks": "every week" if every_week else "the latest week with saved advice",
            "runs": len(entries),
            "calls": [{"run": c.run_id, "kind": c.kind, "player": c.player,
                       "score": _r(c.score), "slot": c.lock_slot or c.slot,
                       "verdict": c.verdict, "your_override": c.override,
                       "next_games": [g.text() for g in c.later], "detail": c.detail}
                      for c in result.calls],
            "tally": tally,
            "overall": {"right": right_total, "graded": graded_total,
                        "percent_right": round(100 * right_total / graded_total)
                        if graded_total else None},
            "overrides": {"beat_the_app": beat, "graded": overridden},
            "forecast_check": None if gap is None else {
                "finished_slots": len(result.forecast_pairs), "average_gap": _r(abs(gap)),
                "direction": "above Exp" if gap >= 0 else "below Exp"},
            "notes": [{"run": e["id"], "slot": n.get("slot"), "text": n["text"]}
                      for e in result.entries for n in e.get("notes", [])],
            "grading_rule": "Every call is graded on the player's next game. LOCK is right if "
                            "that game was lower or he sat out; PASS and HELD are right if it "
                            "beat the score. FREE = he had no games left, so not graded.",
            "warnings": result.lineup_warnings + result.log_warnings,
        }

    def _name(self, pid: str | None) -> str | None:
        return self.team.name(pid) if pid else None


def reply_text(content: dict) -> str:
    """The answer's text, without the model's thinking (parts marked thought)."""
    return "".join(p["text"] for p in content.get("parts", [])
                   if "text" in p and not p.get("thought")).strip()


def _r(x) -> float | None:
    """Numbers in tool results get one decimal, as the app prints them."""
    return None if x is None else round(float(x), 1)


def _day(d: date) -> str:
    """'Fri 10/30'."""
    return f"{d:%a %m/%d}"


def _when(t: datetime) -> str:
    """'Fri 10/30 8:15am'."""
    return f"{t:%a %m/%d} {t.hour % 12 or 12}:{t:%M}{'am' if t.hour < 12 else 'pm'}"


def mentions(team: service.Team, pid: str, text: str) -> bool:
    """Whether `text` names this roster player.

    His last name is enough when no one else on the roster has it ("Clingan").
    When it's shared (Kyshawn and Paul George), it also takes his first name
    ("Kyshawn George") or his initial right before it ("K. George"), so
    "Paul George" doesn't count as naming Kyshawn.
    """
    words = name_words(re.sub(r"['’]s\b", "", text))   # "George's" -> "George"

    def first_and_last(p: str) -> tuple[str, str]:
        rec = team.player(p)
        first = name_words(rec.get("first_name") or "")
        last = name_words(rec.get("last_name") or team.name(p))
        return (first[0] if first else ""), (last[0] if last else "")

    first, last = first_and_last(pid)
    if not last or last not in words:
        return False
    shared = sum(first_and_last(p)[1] == last for p in team.player_ids) > 1
    if not shared:
        return True
    return first in words or any(w == first[:1] and nxt == last
                                 for w, nxt in zip(words, words[1:]))
