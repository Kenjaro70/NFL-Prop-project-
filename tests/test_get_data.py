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


class _FakeResponse:
    """Stands in for urlopen()'s response: serves bytes from a local file."""

    def __init__(self, path):
        self._fh = open(path, "rb")

    def read(self, n=-1):
        return self._fh.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()


def test_bogus_download_is_rejected_and_cleaned_up(get_data, tmp_path, monkeypatch):
    bogus = tmp_path / "bogus.bin"
    bogus.write_bytes(b"<html>rate limited</html>")  # not a parquet file
    monkeypatch.setattr(get_data.urllib.request, "urlopen",
                        lambda url, timeout: _FakeResponse(bogus))
    dest = tmp_path / "out" / "player_stats_2023.parquet"
    dest.parent.mkdir()
    with pytest.raises(Exception):
        get_data.download("https://example.invalid/x.parquet", dest)
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []  # no leftover .part file


def test_good_download_is_verified_then_renamed(get_data, tmp_path, monkeypatch):
    import pandas as pd

    good = tmp_path / "good.parquet"
    pd.DataFrame({"player_id": ["a", "b"]}).to_parquet(good, index=False)
    monkeypatch.setattr(get_data.urllib.request, "urlopen",
                        lambda url, timeout: _FakeResponse(good))
    dest = tmp_path / "out" / "player_stats_2023.parquet"
    dest.parent.mkdir()
    assert get_data.download("https://example.invalid/x.parquet", dest) is True
    assert get_data.verify(dest) == 2
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]
