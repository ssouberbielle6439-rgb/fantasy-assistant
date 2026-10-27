from fantasy_assistant.status import play_chances


def test_healthy():
    assert play_chances(None, 3) == [0.97, 0.97, 0.97]


def test_out_is_next_game_only():
    chances = play_chances("Out", 3)
    assert chances[0] == 0
    assert chances[1] > 0  # he can come back later in the week


def test_status_spelling_variants():
    assert play_chances("GTD", 1) == play_chances("game time decision", 1)
    assert play_chances("OUT", 1) == [0.0]


def test_unknown_status_is_treated_cautiously():
    assert play_chances("Some New Label", 1) == play_chances("GTD", 1)


def test_no_games():
    assert play_chances("Out", 0) == []
