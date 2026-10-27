from fantasy_assistant.nba_stats import normalize_name
from fantasy_assistant.profile import build_profile, estimate_play_prob

SIMPLE = {"pts": 1.0}  # 1 fantasy point per real point keeps the math obvious


def test_normalize_name():
    assert normalize_name("Jabari Smith Jr.") == "jabari smith"
    assert normalize_name("Nikola Jokić") == "nikola jokic"
    assert normalize_name("De'Aaron Fox") == "deaaron fox"
    assert normalize_name("Shai Gilgeous-Alexander") == "shai gilgeous alexander"


def test_profile_average_and_swing():
    boxes = [{"pts": 20}, {"pts": 30}, {"pts": 40}]
    prof = build_profile("Test", boxes, SIMPLE, last_season_games=82)
    assert prof.average == 30
    assert prof.swing == 10
    assert prof.games_used == 3


def test_too_few_games():
    assert build_profile("Test", [{"pts": 20}], SIMPLE, 82) is None


def test_swing_has_a_floor():
    boxes = [{"pts": 30}] * 5  # perfectly consistent
    assert build_profile("Test", boxes, SIMPLE, 82).swing == 4.0


def test_play_prob():
    assert estimate_play_prob(82) == 0.98   # capped: nobody is a lock to play
    assert estimate_play_prob(41) == 0.5
    assert estimate_play_prob(10) == 0.5    # floor
    assert estimate_play_prob(None) == 0.9  # rookies
