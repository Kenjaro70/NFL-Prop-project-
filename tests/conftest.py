"""Shared fixtures: a small, deterministic stats table + the FAKE odds CSV.

The stats fixture mimics the real nflverse player_stats parquet columns.
Every stat follows a simple formula so tests can recompute expected values:
    yards  = base + 5 * ((week + season) % 4)
    counts = base + (week + season) % 3
Stats a player has no base for are 0.

Specials (each exists to exercise one edge case):
  * Mahomes 2022 week 3 is dropped        -> a bye; windows count games, not weeks
  * Kelce 2022 week 2 receiving_yards NaN -> NULL must become 0 in staging
  * McCaffrey 2023 week 19 season_type POST -> must be filtered out
  * Justin Tucker (K)                     -> non-skill position, filtered out
  * Marquise Brown ARI -> KC in 2023 week 4 -> traded player
  * two different "Mike Williams" on LAC in 2023 -> ambiguous name

Raw rows = 114; stg_player_week must have 101 (8 players x 12 - 1 bye + 6).
"""

from pathlib import Path

import pandas as pd
import pytest

from src.pipeline import run_pipeline

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ODDS_CSV = FIXTURES / "FAKE_odds.csv"

YARD_COLS = ["passing_yards", "rushing_yards", "receiving_yards"]
COUNT_COLS = ["completions", "attempts", "carries", "receptions", "targets"]

# (player_id, display name, position, team, base stats)
PLAYERS = [
    ("00-0000001", "Patrick Mahomes", "QB", "KC",
     {"attempts": 34, "completions": 22, "passing_yards": 260, "carries": 4, "rushing_yards": 18}),
    ("00-0000002", "Christian McCaffrey", "RB", "SF",
     {"carries": 18, "rushing_yards": 80, "targets": 6, "receptions": 5, "receiving_yards": 35}),
    ("00-0000003", "DJ Moore", "WR", "CHI",
     {"targets": 9, "receptions": 6, "receiving_yards": 75}),
    ("00-0000004", "Marquise Brown", "WR", "ARI",
     {"targets": 8, "receptions": 5, "receiving_yards": 55}),
    ("00-0000005", "Travis Kelce", "TE", "KC",
     {"targets": 8, "receptions": 5, "receiving_yards": 60}),
    ("00-0000006", "Kenneth Walker III", "RB", "SEA",
     {"carries": 16, "rushing_yards": 70, "targets": 2, "receptions": 1, "receiving_yards": 8}),
    ("00-0000007", "Evan Engram", "TE", "JAX",
     {"targets": 6, "receptions": 4, "receiving_yards": 40}),
    ("00-0000008", "Justin Tucker", "K", "BAL", {}),
    ("00-0000009", "Mike Williams", "WR", "LAC",
     {"targets": 7, "receptions": 4, "receiving_yards": 50}),
    ("00-0000010", "Mike Williams", "WR", "LAC",
     {"targets": 1, "receptions": 1, "receiving_yards": 6}),
]

POSITION_GROUP = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "SPEC"}


def _row(pid, name, pos, team, base, season, week, season_type="REG"):
    row = {
        "player_id": pid,
        "player_name": name,  # real data abbreviates (e.g. "P.Mahomes"); unused here
        "player_display_name": name,
        "position": pos,
        "position_group": POSITION_GROUP[pos],
        "recent_team": team,
        "season": season,
        "week": week,
        "season_type": season_type,
        "opponent_team": "NYJ",
    }
    for col in YARD_COLS:
        row[col] = float(base[col] + 5 * ((week + season) % 4)) if col in base else 0.0
    for col in COUNT_COLS:
        row[col] = int(base[col] + (week + season) % 3) if col in base else 0
    return row


def make_stats_frame() -> pd.DataFrame:
    rows = []
    for pid, name, pos, team, base in PLAYERS:
        for season in (2022, 2023):
            if pid == "00-0000010" and season == 2022:
                continue  # the second Mike Williams only exists in 2023
            for week in range(1, 7):
                if pid == "00-0000001" and season == 2022 and week == 3:
                    continue  # Mahomes bye
                row_team = team
                if pid == "00-0000004" and season == 2023 and week >= 4:
                    row_team = "KC"  # traded
                rows.append(_row(pid, name, pos, row_team, base, season, week))
    # one playoff game that staging must drop
    mccaffrey = PLAYERS[1]
    rows.append(_row(*mccaffrey[:5], 2023, 19, season_type="POST"))

    df = pd.DataFrame(rows)
    df["season"] = df["season"].astype("int32")
    df["week"] = df["week"].astype("int32")
    for col in COUNT_COLS:
        df[col] = df[col].astype("int32")
    kelce = (df.player_id == "00-0000005") & (df.season == 2022) & (df.week == 2)
    df.loc[kelce, "receiving_yards"] = float("nan")
    return df


@pytest.fixture
def stats_frame() -> pd.DataFrame:
    return make_stats_frame()


@pytest.fixture
def odds_csv() -> Path:
    return ODDS_CSV


@pytest.fixture
def run_on_frame():
    """Factory: write a stats DataFrame to parquet and run the full pipeline."""

    def _run(df: pd.DataFrame, tmp_path: Path):
        stats = Path(tmp_path) / "player_stats_fixture.parquet"
        df.to_parquet(stats, index=False)
        return run_pipeline(stats_path=stats, odds_path=ODDS_CSV)

    return _run


@pytest.fixture(scope="session")
def con(tmp_path_factory):
    """One pipeline run on the fixture data, shared by read-only tests."""
    tmp = tmp_path_factory.mktemp("pipeline")
    stats = tmp / "player_stats_fixture.parquet"
    make_stats_frame().to_parquet(stats, index=False)
    connection = run_pipeline(stats_path=stats, odds_path=ODDS_CSV)
    yield connection
    connection.close()


@pytest.fixture
def feature_cols() -> list[str]:
    return [
        "n_prior_games",
        "avg_pass_yds_l6",
        "avg_rush_yds_l6",
        "avg_carries_l6",
        "avg_rec_yds_l6",
        "avg_targets_l6",
        "avg_receptions_l6",
    ]
