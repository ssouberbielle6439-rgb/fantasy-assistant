from fantasy_assistant.scoring import fantasy_points

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
