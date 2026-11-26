"""Tests for sql/02_features.sql. The main property: no feature for a game may
depend on that game or any later game."""

import numpy as np
import pandas as pd
import pytest

DJ_MOORE = "00-0000003"
MAHOMES = "00-0000001"

# stat in stg_player_week -> feature column in player_features
FEATURE_SOURCES = {
    "avg_pass_yds_l6": "passing_yards",
    "avg_rush_yds_l6": "rushing_yards",
    "avg_carries_l6": "carries",
    "avg_rec_yds_l6": "receiving_yards",
    "avg_targets_l6": "targets",
    "avg_receptions_l6": "receptions",
}


def frame(con, sql):
    return con.sql(sql).df()


def features(con):
    return frame(con, "SELECT * FROM player_features ORDER BY player_id, season, week")


def test_features_one_row_per_player_week(con):
    f = features(con)
    assert len(f) == 101
    assert not f.duplicated(["player_id", "season", "week"]).any()


def test_rolling_avgs_match_manual_strictly_earlier_games(con):
    stg = frame(con, "SELECT * FROM stg_player_week ORDER BY player_id, season, week")
    f = features(con)
    for feat, src in FEATURE_SOURCES.items():
        # shift(1) drops the current game; rolling(6) = last 6 earlier games
        expected = stg.groupby("player_id")[src].transform(
            lambda s: s.astype(float).shift(1).rolling(6, min_periods=1).mean()
        )
        got = f[feat].astype(float)
        # NaN (pandas) <-> NULL (SQL) on each player's first game
        np.testing.assert_allclose(got.to_numpy(), expected.to_numpy(), equal_nan=True)
    expected_n = stg.groupby("player_id").cumcount().clip(upper=6)
    assert (f["n_prior_games"].to_numpy() == expected_n.to_numpy()).all()


def test_first_game_has_no_history(con, feature_cols):
    first = frame(con, "SELECT * FROM player_features WHERE game_seq = 1")
    assert len(first) == 9  # one first game per skill player
    assert (first["n_prior_games"] == 0).all()
    for col in feature_cols[1:]:
        assert first[col].isna().all(), col


def test_window_counts_games_not_weeks(con):
    # Mahomes had a bye in 2022 week 3, so week 4 has only 2 prior games
    [(n, avg)] = con.execute(f"""
        SELECT n_prior_games, avg_pass_yds_l6 FROM player_features
        WHERE player_id = '{MAHOMES}' AND season = 2022 AND week = 4
    """).fetchall()
    wk1 = 260 + 5 * ((1 + 2022) % 4)
    wk2 = 260 + 5 * ((2 + 2022) % 4)
    assert n == 2
    assert avg == pytest.approx((wk1 + wk2) / 2)


def test_window_crosses_seasons(con):
    [(n, avg)] = con.execute(f"""
        SELECT n_prior_games, avg_rec_yds_l6 FROM player_features
        WHERE player_id = '{DJ_MOORE}' AND season = 2023 AND week = 1
    """).fetchall()
    expected = np.mean([75 + 5 * ((w + 2022) % 4) for w in range(1, 7)])
    assert n == 6
    assert avg == pytest.approx(expected)


def test_mutating_a_game_never_changes_its_own_or_earlier_features(
    stats_frame, run_on_frame, tmp_path, feature_cols
):
    (tmp_path / "base").mkdir()
    (tmp_path / "bumped").mkdir()
    base_con = run_on_frame(stats_frame, tmp_path / "base")
    bumped = stats_frame.copy()
    later = (bumped.player_id == DJ_MOORE) & (bumped.season * 100 + bumped.week >= 202304)
    bumped.loc[later, ["receiving_yards"]] += 500.0
    bumped.loc[later, ["receptions", "targets"]] += 10
    new_con = run_on_frame(bumped, tmp_path / "bumped")

    def feats(c):
        return frame(c, f"""
            SELECT * FROM player_features
            WHERE player_id = '{DJ_MOORE}' ORDER BY season, week""").set_index(["season", "week"])

    def projs(c):
        return frame(c, """
            SELECT player_id, season, week, market, week_index, projection, model_sd
            FROM player_projections
            ORDER BY player_id, season, week, market""")

    old, new = feats(base_con), feats(new_con)
    early = old.index.map(lambda sw: sw[0] * 100 + sw[1] <= 202304)
    pd.testing.assert_frame_equal(old.loc[early, feature_cols], new.loc[early, feature_cols])
    # the week after the change does see it
    assert new.loc[(2023, 5), "avg_rec_yds_l6"] > old.loc[(2023, 5), "avg_rec_yds_l6"]

    # projections and every model_sd (all players) up to the changed week are unchanged
    old_p, new_p = projs(base_con), projs(new_con)
    keep = old_p.week_index <= 202304
    pd.testing.assert_frame_equal(
        old_p[keep].reset_index(drop=True), new_p[new_p.week_index <= 202304].reset_index(drop=True)
    )
    base_con.close()
    new_con.close()


def test_projections_one_row_per_player_week_market(con):
    p = frame(con, "SELECT player_id, season, week, market, position FROM player_projections")
    assert not p.duplicated(["player_id", "season", "week", "market"]).any()
    eligible = {
        "pass_yds": {"QB"},
        "rush_yds": {"QB", "RB"},
        "rec_yds": {"RB", "WR", "TE"},
        "receptions": {"RB", "WR", "TE"},
    }
    for market, positions in eligible.items():
        assert set(p.loc[p.market == market, "position"]) == positions, market
    # rows per market = stg rows at the eligible positions
    counts = p.groupby("market").size().to_dict()
    stg = frame(con, "SELECT position, count(*) AS n FROM stg_player_week GROUP BY position")
    per_pos = dict(zip(stg.position, stg.n))
    for market, positions in eligible.items():
        assert counts[market] == sum(per_pos[pos] for pos in positions), market


def test_projection_needs_three_prior_games_and_sd_needs_five_residuals(con):
    p = frame(con, "SELECT * FROM player_projections")
    assert p.loc[p.n_prior_games < 3, "projection"].isna().all()
    assert p.loc[p.n_prior_games >= 3, "projection"].notna().all()
    assert p.loc[p.n_sd_obs < 5, "model_sd"].isna().all()
