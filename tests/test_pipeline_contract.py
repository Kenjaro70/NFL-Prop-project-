"""Contract tests for src/pipeline.py: ordering, path-free SQL, UDFs, checks."""

import math

import duckdb
import pytest

from src import pipeline


def test_sql_files_run_in_numeric_order():
    names = [p.name for p in pipeline.sql_files()]
    assert names == sorted(names)
    assert names[:4] == ["01_staging.sql", "02_features.sql", "03_market.sql", "04_eval.sql"]


def test_sql_files_contain_no_paths():
    # Only pipeline.py knows where data lives; SQL reads the raw_* views.
    for path in pipeline.sql_files():
        text = path.read_text().lower()
        for forbidden in ("read_parquet", "read_csv", ".parquet", ".csv", "data/"):
            assert forbidden not in text, f"{path.name} contains {forbidden!r}"


def test_udfs_registered():
    con = duckdb.connect()
    pipeline.register_udfs(con)
    assert con.execute("SELECT build_key('D.J. Moore', 'JAC')").fetchall()[0][0] == "djmoore|JAX"
    assert con.execute("SELECT normalize_team('zzz')").fetchall()[0][0] is None
    assert con.execute("SELECT canonical_name('Hollywood Brown')").fetchall()[0][0] == "marquisebrown"
    assert con.execute("SELECT normal_cdf(NULL)").fetchall()[0][0] is None


def test_normal_cdf_known_values():
    assert pipeline.normal_cdf(0.0) == pytest.approx(0.5)
    assert pipeline.normal_cdf(1.96) == pytest.approx(0.975, abs=1e-4)
    assert pipeline.normal_cdf(-1.0) == pytest.approx(0.158655, abs=1e-6)
    assert pipeline.normal_cdf(None) is None
    con = duckdb.connect()
    pipeline.register_udfs(con)
    assert con.execute("SELECT normal_cdf(1.0)").fetchall()[0][0] == pytest.approx(
        0.5 * (1 + math.erf(1 / math.sqrt(2)))
    )


def test_missing_stats_raises_helpful_error(tmp_path, odds_csv):
    with pytest.raises(FileNotFoundError, match="make data"):
        pipeline.run_pipeline(stats_path=tmp_path / "nope_*.parquet", odds_path=odds_csv)


def test_run_checks_pass_on_fixture(con):
    pipeline.run_checks(con)  # raises AssertionError on any failure
