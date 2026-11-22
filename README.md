# fantasy-assistant

In Sleeper's **Lock-In Mode** for fantasy basketball, only one game per player counts each week. After each game you choose: **lock** the score, or **pass** and let his next game replace it, with no going back. This app treats that as an *optimal stopping* problem. It reads my real league from Sleeper, forecasts every player's remaining games, and tells me what to lock, which slot to lock it in, and why. A Gemini chat lets me ask about it in plain English, and every number in its answers comes from the app.

**Built with:** Python, Sleeper API, Gemini function calling, pytest (242 tests)

## Example

Real answers from the `chat` command on my league (Sunday, November 22):

```text
You > Should I lock anyone tonight?
App > LOCK Immanuel Quickley in SG (below your 25 target).
      You also have Jabari Smith Jr. at 16.2 and Kyshawn George at 19.6 held
      back by today's 20-point floor.
      (from: get_advice)

You > Which of my players are injured?
App > Jusuf Nurkić is Out and on IR. Derrick White is GTD. Nic Claxton is GTD.
      Jalen Smith is GTD. Evan Mobley is Out and on IR. Jalen Duren is Out and on IR.
      (from: get_injuries)

You > How did the app's advice do this week?
App > The app is 73 percent right across 15 graded calls, with 11 right and
      4 wrong. Lock calls are 100 percent right, Pass calls are 69 percent
      right, and Held calls are 100 percent right.
      (from: review)
```

## How it works

```text
Sleeper API: league, scoring rules, lineup, box scores, live game status
     |
     v
service.py, the engine
  - each player: optimal stopping by backward induction gives the score
    I'd expect to end up locking from his games left this week
  - whole lineup: Hungarian algorithm matches players to open slots,
    which decides whether to lock a score and in which slot
     |
     +--> CLI: advise, review, week, profiles, ...
     +--> Gemini chat: picks the app's tools by function calling
```

- **The math decides, the AI explains.** Gemini can only choose which tools to run and put their results into words. It is told to use no number a tool didn't return and to do no arithmetic.
- **A number check backs that up in code.** Any decimal in an answer that no tool returned gets flagged under the answer. A label check does the same for the "below your target" and "floor" warnings.
- **Each slot's score is computed from box scores**, because Sleeper's API showed the wrong game for locked slots. Sleeper supplies only who is in each slot.

## How it's tested

- **242 automated tests** (pytest, about 2 seconds). Every bug found in my real league gets a regression test named after the situation, such as `test_wednesday_11_18_smith_jr_in_2nd_quarter_stays_in_f_no_free_roll_for_marshall`.
- **Profile backtest over 1,825 games.** `scripts/backtest_profiles.py` replays this season for all 179 rostered players, forecasting each game from only the games before it. The weighting it picked cut forecast bias from +0.58 to +0.23 points per game.
- **Record and replay for the refactor.** Before moving the engine into `service.py`, I recorded every API response during real runs with games live, then replayed them through the old and new code. Output and saved files matched for 6 commands and 15 edge cases.
- **Live checks against Sleeper.** Our scoring matches Sleeper's to the decimal (104 of 108 starter slots league-wide in week 2, and the other 4 were correct zeros for players who sat out). I polled 15 live games every 2 minutes to learn how game status and stats change, which is how the app knows when a score is final.
- **Every piece of advice is logged and graded** against the game that followed (`review`), including the calls I overrode.

## Vision and roadmap

- **Win probability:** simulate both teams' weeks and play to win the matchup, not just to score points. Lock safer when ahead, chase upside when behind.
- **Injury news:** have Gemini read the official NBA injury report and news to sharpen each player's chance to play, with a source link for every claim.
- **Web app:** a FastAPI site with a Lineup tab and a Chat tab, plus a demo mode with a frozen sample week so anyone can click through without a Sleeper account.

Done so far: league and scoring from Sleeper, the lock-in engine, whole-lineup advice, the advice log and grading, box-score slot scores, live tip-off detection, the tuned player averages, and the AI chat.

## Setup and commands

Windows, PowerShell, Python 3.13:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env       # add your Sleeper username, league ID and Gemini key
python -m pytest
```

| Command | What it does |
|---|---|
| `python -m fantasy_assistant advise` | LOCK or PASS for every slot, saved to the advice log |
| `python -m fantasy_assistant chat` | Ask questions in plain English |
| `python -m fantasy_assistant review` | Grade this week's saved advice |
| `python -m fantasy_assistant week` | Who plays which day |
| `python -m fantasy_assistant profiles` | Each player's average, swing and chance to play |
| `python -m fantasy_assistant roster` | League and roster |

Data: [Sleeper API](https://docs.sleeper.com/), with NBA.com via [nba_api](https://github.com/swar/nba_api) as the fallback. License: MIT.
