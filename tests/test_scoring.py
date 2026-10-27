from fantasy_assistant.scoring import fantasy_points, unknown_scoring_keys

# Santiago's league scoring, as returned by Sleeper.
LEAGUE = {
    "ast": 1.0, "blk": 2.0, "bonus_pt_40p": 2.0, "bonus_pt_50p": 3.0,
    "bonus_reb_20p": 3.0, "dd": 3.0, "ff": -2.0, "fgm": 0.1, "ftmi": -0.5,
    "oreb": 0.2, "pf": -0.5, "pts": 0.5, "reb": 1.0, "stl": 2.0, "td": 5.0,
    "tf": -2.0, "to": -1.0, "tpm": 0.5,
}


def test_typical_double_double():
    box = {"pts": 30, "reb": 10, "oreb": 3, "ast": 5, "stl": 2, "blk": 1,
           "tov": 3, "pf": 2, "fgm": 11, "fta": 8, "ftm": 6, "fg3m": 3}
    # 15 pts + 10 reb + 5 ast + 4 stl + 2 blk - 3 to - 1 pf + 1.1 fgm
    # - 1 ft missed + 1.5 threes + 0.6 oreb + 3 double-double = 37.2
    assert fantasy_points(box, LEAGUE) == 37.2


def test_empty_box_is_zero():
    assert fantasy_points({}, LEAGUE) == 0


def test_triple_double_gets_both_bonuses():
    box = {"pts": 10, "reb": 10, "ast": 10}
    # 5 + 10 + 10 + 3 dd + 5 td
    assert fantasy_points(box, LEAGUE) == 33


def test_fifty_point_game_bonuses():
    box = {"pts": 50}
    # 25 + 2 (40+) + 3 (50+)
    assert fantasy_points(box, LEAGUE) == 30


def test_matches_sleeper_jamal_murray_real_games():
    # Real box scores from NBA.com, checked against the FPTS Sleeper shows
    # in Murray's game log: 32.30 (@GSW 10/23/2025) and 33.10 (@MIN 10/27/2025).
    at_gsw = {"pts": 25, "reb": 3, "oreb": 1, "ast": 10, "stl": 3, "blk": 0, "tov": 3,
              "pf": 2, "fgm": 11, "fga": 20, "fg3m": 1, "ftm": 2, "fta": 2}
    at_min = {"pts": 43, "reb": 6, "oreb": 0, "ast": 3, "stl": 0, "blk": 0, "tov": 2,
              "pf": 2, "fgm": 16, "fga": 29, "fg3m": 5, "ftm": 6, "fta": 7}
    assert fantasy_points(at_gsw, LEAGUE) == 32.3
    assert fantasy_points(at_min, LEAGUE) == 33.1


def test_new_assist_bonus_and_lower_double_bonuses():
    # Commissioner change, Oct 2026: 15+ assists +3, double-double 2, triple-double 4.
    updated = {**LEAGUE, "dd": 2.0, "td": 4.0, "bonus_ast_15p": 3.0}
    box = {"pts": 10, "reb": 10, "ast": 15}
    # 5 + 10 + 15 + 2 dd + 4 td + 3 (15+ assists)
    assert fantasy_points(box, updated) == 39
    assert fantasy_points({"ast": 14}, updated) == 14  # just short of the bonus


def test_unknown_scoring_keys_are_reported():
    assert unknown_scoring_keys(LEAGUE) == []
    assert unknown_scoring_keys({**LEAGUE, "bonus_ast_15p": 3.0}) == []
    assert unknown_scoring_keys({**LEAGUE, "some_new_rule": 1.0}) == ["some_new_rule"]
