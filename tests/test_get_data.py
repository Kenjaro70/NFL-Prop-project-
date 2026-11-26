"""Offline tests for scripts/get_data.py (no network)."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "get_data.py"


@pytest.fixture(scope="module")
def get_data():
    spec = importlib.util.spec_from_file_location("get_data", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_url_for_season(get_data):
    assert get_data.url_for_season(2023) == (
        "https://github.com/nflverse/nflverse-data/releases/download/"
        "player_stats/player_stats_2023.parquet"
    )


def test_dest_for_season(get_data, tmp_path):
    assert get_data.dest_for_season(2021, tmp_path) == tmp_path / "player_stats_2021.parquet"


def test_download_skips_existing_file(get_data, tmp_path, monkeypatch):
    dest = tmp_path / "player_stats_2023.parquet"
    dest.write_bytes(b"already here")

    def boom(*args, **kwargs):
        raise AssertionError("urlopen must not be called for an existing file")

    monkeypatch.setattr(get_data.urllib.request, "urlopen", boom)
    assert get_data.download("https://example.invalid/x.parquet", dest) is False
    assert dest.read_bytes() == b"already here"
