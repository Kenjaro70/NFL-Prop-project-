"""Download nflverse weekly player stats (one parquet file per season).

Source: the legacy nflverse release "player_stats", one file per season:
    https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats_{season}.parquet

nflverse also publishes a newer release (stats_player/stats_player_week_{season}.parquet)
with DIFFERENT column names. It is NOT used here: the SQL in sql/ is written
against the legacy columns (recent_team, opponent_team, ...).

Usage:
    python3 scripts/get_data.py                       # 2021-2024 into data/raw/
    python3 scripts/get_data.py --seasons 2023 --force

Uses only the standard library to download, and duckdb to check each file.
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import duckdb

BASE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats_{season}.parquet"
)
DEFAULT_SEASONS = [2021, 2022, 2023, 2024]
RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"


def url_for_season(season: int) -> str:
    return BASE_URL.format(season=season)


def dest_for_season(season: int, out_dir: Path) -> Path:
    return Path(out_dir) / f"player_stats_{season}.parquet"


def download(url: str, dest: Path, force: bool = False) -> bool:
    """Download url to dest. Returns False (and does nothing) if dest exists.

    Writes to a ".part" file first and renames at the end, so an interrupted
    download never leaves a half-written parquet behind.
    """
    dest = Path(dest)
    if dest.exists() and not force:
        return False
    part = dest.with_suffix(".parquet.part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    part.replace(dest)
    return True


def verify(dest: Path) -> int:
    """Return the row count of a parquet file; raise if it has no rows."""
    path = str(Path(dest).resolve()).replace("'", "''")
    con = duckdb.connect()
    try:
        [(n,)] = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchall()
    finally:
        con.close()
    if n == 0:
        raise ValueError(f"{dest} has 0 rows")
    return n


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seasons", nargs="+", type=int, default=DEFAULT_SEASONS)
    parser.add_argument("--out", type=Path, default=RAW_DIR)
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    for season in args.seasons:
        dest = dest_for_season(season, args.out)
        fetched = download(url_for_season(season), dest, force=args.force)
        rows = verify(dest)
        status = "downloaded" if fetched else "cached"
        print(f"{season}: {rows:>6} rows  ({status})  {dest}")


if __name__ == "__main__":
    main()
