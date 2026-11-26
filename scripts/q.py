"""Run a SQL query against the pipeline database (read-only) and print it.

    python3 scripts/q.py "SELECT * FROM stg_player_week LIMIT 5"
    python3 scripts/q.py my_query.sql

Build the database first with `make data && make pipeline`.
The tables are plain tables, so no Python UDFs are needed to read them.
"""

import sys
from pathlib import Path

import duckdb

DB = Path(__file__).resolve().parents[1] / "data" / "nfl.duckdb"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    arg = sys.argv[1]
    sql = Path(arg).read_text() if arg.endswith(".sql") else arg
    if not DB.exists():
        sys.exit(f"{DB} not found - run `make data && make pipeline` first")
    con = duckdb.connect(str(DB), read_only=True)
    con.sql(sql).show(max_rows=60, max_width=160)


if __name__ == "__main__":
    main()
