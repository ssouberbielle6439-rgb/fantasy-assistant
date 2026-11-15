from statistics import mean

import pytest

from fantasy_assistant.nba_stats import normalize_name
from fantasy_assistant.profile import (build_profile, estimate_play_prob,
                                       last_season_weight, weighted_average)

SIMPLE = {"pts": 1.0}  # 1 fantasy point per real point keeps the math obvious


def boxes(points):
    """Box scores that score exactly these fantasy points under SIMPLE."""
    return [{"pts": p} for p in points]


def test_normalize_name():
    assert normalize_name("Jabari Smith Jr.") == "jabari smith"
    assert normalize_name("Nikola Jokić") == "nikola jokic"
    assert normalize_name("De'Aaron Fox") == "deaaron fox"
    assert normalize_name("Shai Gilgeous-Alexander") == "shai gilgeous alexander"


def test_profile_average_and_swing():
    prof = build_profile("Test", boxes([20, 30, 40]), [], SIMPLE)
    assert prof.average == 30
    assert prof.swing == 10
    assert prof.games_used == 3


def test_too_few_games():
    assert build_profile("Test", boxes([20]), [], SIMPLE) is None
    assert build_profile("Test", boxes([20]), boxes([25]), SIMPLE) is None


def test_swing_has_a_floor():
    assert build_profile("Test", boxes([30] * 5), [], SIMPLE).swing == 4.0


def test_play_prob():
    assert estimate_play_prob(82) == 0.98   # capped: nobody is a lock to play
    assert estimate_play_prob(41) == 0.5
    assert estimate_play_prob(10) == 0.5    # floor
    assert estimate_play_prob(None) == 0.9  # rookies
    assert estimate_play_prob(0) == 0.9


def test_play_prob_comes_from_last_seasons_games_played():
    prof = build_profile("Test", boxes([20] * 3), boxes([20] * 41), SIMPLE)
    assert prof.play_prob == 0.5


# ---------- Weighting this season more as it goes on (11/15) ----------

def test_last_season_counts_as_5_games_fading_to_0_by_game_25():
    assert last_season_weight(0) == 5
    assert last_season_weight(3) == pytest.approx(4.4)
    assert last_season_weight(10) == pytest.approx(3)
    assert last_season_weight(20) == pytest.approx(1)
    assert last_season_weight(25) == 0
    assert last_season_weight(40) == 0     # never negative


def test_weighted_average_worked_example():
    # 10 games averaging 20, last season averaged 30, weight 3:
    # (200 + 3 * 30) / (10 + 3) = 22.3
    assert weighted_average([20] * 10, [30] * 25) == pytest.approx(290 / 13)


def test_opening_night_uses_last_seasons_average():
    assert weighted_average([], [10, 20, 30]) == 20


def test_last_season_average_uses_his_last_25_games_of_it():
    last = [30] * 25 + [0] * 50              # most recent first: the 0s are older
    assert weighted_average([], last) == 30


def test_rookie_average_is_this_season_only():
    assert weighted_average([10, 20, 30], []) == 20
    assert build_profile("Rookie", boxes([10, 20, 30]), [], SIMPLE).average == 20


def test_from_game_25_on_last_season_is_ignored():
    this = [20] * 30
    assert weighted_average(this, [50] * 82) == 20


def test_only_his_last_25_games_this_season_count():
    this = [30] * 25 + [10] * 5              # the 10s are older than 25 games back
    assert weighted_average(this, [0] * 82) == 30


def test_kyshawn_george_average_follows_his_season_not_the_end_of_last_season():
    # His real games (Sleeper, scored with the league's settings), most recent
    # first, through 11/12. He averaged 18.6 over 13 games this season. The old
    # average filled out 25 games with the final 12 of last season, which
    # averaged only 13.3, so it said 16.0 and the plan kept expecting less
    # from him than he was scoring.
    this_season = [12.5, 21.6, 9.5, 24.9, 33.7, 19.5, 23.9, 12.9, 18.4, 13.5,
                   29.2, 5.4, 16.3]
    last_season = [13.5, 15.0, 5.3, 14.9, 9.5, 17.0, 12.9, 8.5, 10.8, 5.8, 17.5,
                   28.4, 31.7, 24.1, 29.7, 21.0, 34.7, 17.5, 21.9, 5.7, 14.0, 19.1,
                   3.3, 17.6, 28.4]
    assert round(mean(this_season), 1) == 18.6
    assert round(mean(last_season[:12]), 1) == 13.3
    old_average = mean((this_season + last_season)[:25])
    assert round(old_average, 1) == 16.0

    # Now: last season's average (17.1 over its last 25 games) counts as
    # 5 * (1 - 13/25) = 2.4 games next to his 13 this season.
    assert round(mean(last_season), 1) == 17.1

    prof = build_profile("Kyshawn George", boxes(this_season), boxes(last_season), SIMPLE)
    assert prof.average == 18.3
    assert prof.games_used == 25
