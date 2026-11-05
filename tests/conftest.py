import pytest

from fantasy_assistant import advice_log, locks, sleeper_stats


@pytest.fixture(autouse=True)
def keep_real_data_safe(tmp_path, monkeypatch):
    """Every test writes the advice log, lock files and Sleeper stats cache to a
    temporary folder, never to data/ or notes/state/ (Santiago's real season
    record). STATE_DIR is set too, so even code that reads .env stays here:
    load_dotenv never overrides a setting that's already there."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(advice_log, "LOG_PATH", tmp_path / "advice_log.json")
    monkeypatch.setattr(locks, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sleeper_stats, "DATA_DIR", tmp_path / "gamelogs")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never download from Sleeper; they use the saved samples in tests/data/."""
    def refuse(path):
        raise AssertionError(f"test tried to download {path}")
    monkeypatch.setattr(sleeper_stats, "_get_json", refuse)
