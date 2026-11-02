import pytest

from fantasy_assistant import advice_log, locks


@pytest.fixture(autouse=True)
def keep_real_data_safe(tmp_path, monkeypatch):
    """Every test writes the advice log and lock files to a temporary folder,
    never to data/ (that's Santiago's real season record)."""
    monkeypatch.setattr(advice_log, "LOG_PATH", tmp_path / "advice_log.json")
    monkeypatch.setattr(locks, "DATA_DIR", tmp_path)
