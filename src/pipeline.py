"""Run the SQL pipeline: raw files -> DuckDB tables -> checks -> summary.

Steps
  1. register Python UDFs (name/team resolution from src/resolve.py, normal_cdf)
  2. create two views over the raw files: raw_player_stats, raw_odds
  3. run sql/NN_*.sql in numeric order (each builds TABLES, never views,
     because a saved .duckdb file reopens without our Python UDFs)
  4. run sanity checks (each query must return 0)
  5. print a summary

The SQL files contain no file paths; this module is the only place that knows
where data lives.

    python3 -m src.pipeline --db data/nfl.duckdb
"""

from __future__ import annotations

import argparse
import glob
import math
from pathlib import Path

import duckdb

from src import resolve

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql"
DEFAULT_STATS = ROOT / "data" / "raw" / "player_stats_*.parquet"
DEFAULT_ODDS = ROOT / "tests" / "fixtures" / "FAKE_odds.csv"
DEFAULT_DB = ROOT / "data" / "nfl.duckdb"

ODDS_COLUMNS = {
    "source": "VARCHAR",
    "odds_id": "INTEGER",
    "book": "VARCHAR",
    "season": "INTEGER",
    "week": "INTEGER",
    "player_name": "VARCHAR",
    "team": "VARCHAR",
    "market": "VARCHAR",
    "open_line": "DOUBLE",
    "open_over_price": "INTEGER",
    "open_under_price": "INTEGER",
    "close_line": "DOUBLE",
    "close_over_price": "INTEGER",
    "close_under_price": "INTEGER",
}


def normal_cdf(x: float | None) -> float | None:
    """Standard normal CDF. DuckDB has no erf/normal CDF built in."""
    if x is None:
        return None
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def register_udfs(con: duckdb.DuckDBPyConnection) -> None:
    """Expose Python functions to SQL.

    null_handling="special" means DuckDB passes NULLs into the function (and
    accepts None back) instead of erroring when a UDF returns None.
    """
    udfs = [
        ("normalize_name", resolve.normalize_name, ["VARCHAR"], "VARCHAR"),
        ("normalize_team", resolve.normalize_team, ["VARCHAR"], "VARCHAR"),
        ("canonical_name", resolve.canonical_name, ["VARCHAR"], "VARCHAR"),
        ("build_key", resolve.build_key, ["VARCHAR", "VARCHAR"], "VARCHAR"),
        ("normal_cdf", normal_cdf, ["DOUBLE"], "DOUBLE"),
    ]
    for name, fn, params, returns in udfs:
        con.create_function(name, fn, params, returns, null_handling="special")


def _sql_str(path: Path | str) -> str:
    """Absolute path as the inside of a SQL string literal (' doubled)."""
    return str(Path(path).resolve()).replace("'", "''")


def create_raw_views(con, stats_path, odds_path) -> None:
    if not glob.glob(str(stats_path)):
        raise FileNotFoundError(
            f"No stats parquet at {stats_path} - run `make data` first"
        )
    con.execute(
        "CREATE OR REPLACE VIEW raw_player_stats AS "
        f"SELECT * FROM read_parquet('{_sql_str(stats_path)}', union_by_name = true)"
    )
    columns = ", ".join(f"'{col}': '{typ}'" for col, typ in ODDS_COLUMNS.items())
    con.execute(
        "CREATE OR REPLACE VIEW raw_odds AS "
        f"SELECT * FROM read_csv('{_sql_str(odds_path)}', header = true, "
        f"columns = {{{columns}}})"
    )


def sql_files(sql_dir: Path = SQL_DIR) -> list[Path]:
    return sorted(Path(sql_dir).glob("[0-9][0-9]_*.sql"))


def run_sql_files(con, sql_dir: Path = SQL_DIR) -> list[str]:
    names = []
    for path in sql_files(sql_dir):
        try:
            con.execute(path.read_text())
        except duckdb.Error as exc:
            raise RuntimeError(f"{path.name} failed: {exc}") from exc
        names.append(path.name)
    return names


# Each query must return 0. A non-zero count means the pipeline is wrong.
CHECKS = [
    (
        "stg_player_week_dupes",
        """SELECT count(*) FROM (
               SELECT player_id, season, week FROM stg_player_week
               GROUP BY ALL HAVING count(*) > 1)""",
    ),
    (
        "features_rowcount_mismatch",
        """SELECT abs((SELECT count(*) FROM player_features)
                    - (SELECT count(*) FROM stg_player_week))""",
    ),
    (
        "projection_dupes",
        """SELECT count(*) FROM (
               SELECT player_id, season, week, market FROM player_projections
               GROUP BY ALL HAVING count(*) > 1)""",
    ),
    (
        "market_fanout",
        """SELECT abs((SELECT count(*) FROM market_lines)
                    - (SELECT count(*) FROM stg_odds))""",
    ),
    (
        "bet_rowcount_mismatch",
        """SELECT abs((SELECT count(*) FROM bet_results)
                    - (SELECT count(*) FROM market_lines WHERE match_status = 'matched'))""",
    ),
    # odds_id is the grain of these three tables. The fan-out check above only
    # compares counts, so a CSV that repeats an odds_id would slip through it.
    *(
        (
            f"{table}_odds_id_dupes",
            f"SELECT count(*) - count(DISTINCT odds_id) FROM {table}",
        )
        for table in ("stg_odds", "market_lines", "bet_results")
    ),
    (
        "bet_results_ungraded",
        "SELECT count(*) FROM bet_results WHERE result IS NULL",
    ),
]


def run_checks(con) -> None:
    # fetchall(), not fetchone(): in DuckDB 2.x a half-read result blocks the
    # connection for later con.sql() calls.
    failures = []
    for name, query in CHECKS:
        [(value,)] = con.execute(query).fetchall()
        if value != 0:
            failures.append(f"{name}: {value}")
    if failures:
        raise AssertionError("pipeline checks failed: " + "; ".join(failures))


def run_pipeline(stats_path=DEFAULT_STATS, odds_path=DEFAULT_ODDS, db_path=":memory:"):
    con = duckdb.connect(str(db_path))
    try:
        register_udfs(con)
        create_raw_views(con, stats_path, odds_path)
        run_sql_files(con)
        run_checks(con)
    except BaseException:
        con.close()  # don't leak an open connection (or a locked .duckdb file)
        raise
    return con


SUMMARY_TABLES = [
    "stg_player_week",
    "player_name_xwalk",
    "player_features",
    "player_projections",
    "stg_odds",
    "market_lines",
    "bet_results",
    "clv_summary",
    "calibration_buckets",
]


def summarize(con) -> None:
    print("row counts")
    for table in SUMMARY_TABLES:
        [(n,)] = con.execute(f"SELECT count(*) FROM {table}").fetchall()
        print(f"  {table:<22} {n:>8}")

    print("match_status")
    for status, n in con.execute(
        "SELECT match_status, count(*) FROM market_lines GROUP BY ALL ORDER BY 2 DESC, 1"
    ).fetchall():
        print(f"  {status:<22} {n:>8}")

    odds_keys = [r[0] for r in con.execute(
        "SELECT DISTINCT season || ':' || join_key FROM stg_odds").fetchall()]
    xwalk_keys = [r[0] for r in con.execute(
        "SELECT DISTINCT season || ':' || join_key FROM player_name_xwalk").fetchall()]
    resolve.report_unmatched(odds_keys, xwalk_keys, label="odds")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the NFL prop DuckDB tables.")
    parser.add_argument("--stats", default=str(DEFAULT_STATS))
    parser.add_argument("--odds", default=str(DEFAULT_ODDS))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args(argv)

    if "FAKE" in Path(args.odds).name:
        bar = "*" * 64
        print(f"{bar}\n*** USING FAKE ODDS FIXTURE - CLV/ROI NUMBERS ARE MEANINGLESS ***\n{bar}")

    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = run_pipeline(args.stats, args.odds, args.db)
    try:
        summarize(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
