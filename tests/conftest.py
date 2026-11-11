import pytest

from kabu import config


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """テストが data/ フォルダを汚さないようにする。J-Quants は使わない。"""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DECISIONS_FILE", tmp_path / "decisions.json")
    monkeypatch.setattr(config, "JQUANTS_API_KEY", "")
    yield tmp_path
