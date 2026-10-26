from fantasy_assistant.lockin import GameForecast, decide, expected_max, value_of_waiting

AVG = GameForecast(mean=40, sd=10, play_prob=0.95)


def test_no_games_left_means_always_lock():
    # Nothing to wait for, so any non-negative score should be locked.
    assert value_of_waiting([]) == 0
    assert decide(5, []).lock


def test_expected_max_with_no_spread():
    assert expected_max(40, 0, 30) == 40
    assert expected_max(40, 0, 50) == 50


def test_expected_max_is_at_least_both_inputs():
    assert expected_max(40, 10, 45) >= 45
    assert expected_max(40, 10, 45) >= 40


def test_threshold_rises_with_more_games_left():
    one = value_of_waiting([AVG])
    two = value_of_waiting([AVG, AVG])
    three = value_of_waiting([AVG, AVG, AVG])
    assert one < two < three


def test_known_thresholds():
    # Worked example: 40-point average, swings of about 10, 95% to play.
    assert round(value_of_waiting([AVG]), 1) == 38.0
    assert round(value_of_waiting([AVG, AVG]), 1) == 42.8
    assert round(value_of_waiting([AVG, AVG, AVG]), 1) == 45.4


def test_rest_risk_lowers_threshold():
    healthy = value_of_waiting([GameForecast(40, 10, 1.0)])
    shaky = value_of_waiting([GameForecast(40, 10, 0.5)])
    assert shaky < healthy


def test_decision():
    assert decide(44, [AVG]).lock          # 44 beats ~38
    assert not decide(44, [AVG, AVG, AVG]).lock  # 44 doesn't beat ~45.4
