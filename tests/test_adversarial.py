"""Adversarial tests: leakage under heavy mutation, duplicate inputs, edge cases,
and row counts (no fan-out) on a fixture with extra trades / name collisions.

All offline: the stats come from the `stats_frame` fixture (modified per test)
and the odds from the FAKE fixture or small FAKE CSVs written to tmp_path.
"""

import csv
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import pipeline

YARD_COLS = ["passing_yards", "rushing_yards", "receiving_yards"]
COUNT_COLS = ["completions", "attempts", "carries", "receptions", "targets"]
STAT_COLS = YARD_COLS + COUNT_COLS

ODDS_HEADER = [
    "source", "odds_id", "book", "season", "week", "player_name", "team", "market",
    "open_line", "open_over_price", "open_under_price",
    "close_line", "close_over_price", "close_under_price",
]

DJ_MOORE = "00-0000003"
KELCE = "00-0000005"

FEATURE_KEYS = ["player_id", "season", "week"]
PROJ_KEYS = ["player_id", "season", "week", "market"]
PROJ_PRE_GAME = ["week_index", "position", "team", "n_prior_games", "projection",
                 "sd_prior", "n_sd_obs", "model_sd"]
ML_PRE_GAME = ["odds_id", "match_status", "player_id", "season", "week", "market",
               "open_line", "projection", "model_sd", "n_prior_games", "p_over_model"]


# --------------------------------------------------------------------------- helpers
def frame(con, sql):
    return con.sql(sql).df()


def write_stats(df, tmp_path) -> Path:
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "player_stats_adv.parquet"
    df.to_parquet(path, index=False)
    return path


def write_odds(rows, tmp_path) -> Path:
    """rows: list of dicts with at least odds_id, season, week, player_name, team,
    market, open_line. Prices / close default to a flat -110 market at the same line."""
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "FAKE_adv_odds.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ODDS_HEADER)
        w.writeheader()
        for r in rows:
            full = {
                "source": "FAKE", "book": "fakebook",
                "open_over_price": -110, "open_under_price": -110,
                "close_over_price": -110, "close_under_price": -110,
                **r,
            }
            full.setdefault("close_line", full["open_line"])
            w.writerow(full)
    return path


def run(df, tmp_path, odds_path=None):
    stats = write_stats(df, tmp_path)
    if odds_path is None:
        odds_path = pipeline.DEFAULT_ODDS  # tests/fixtures/FAKE_odds.csv
    return pipeline.run_pipeline(stats_path=stats, odds_path=odds_path)


def clone_player(df, src_id, new_id, name=None, team=None, keep=None):
    """Copy every raw row of `src_id` as a new player (optionally renamed /
    re-teamed / filtered by a boolean function of the copied frame)."""
    rows = df[df.player_id == src_id].copy()
    rows["player_id"] = new_id
    if name is not None:
        rows["player_name"] = name
        rows["player_display_name"] = name
    if team is not None:
        rows["recent_team"] = team
    if keep is not None:
        rows = rows[keep(rows)]
    return rows


def wi(df):
    return df["season"] * 100 + df["week"]


def assert_same(old, new, keys, cols, label):
    old = old.sort_values(keys).reset_index(drop=True)[keys + cols]
    new = new.sort_values(keys).reset_index(drop=True)[keys + cols]
    assert len(old) == len(new), f"{label}: row count {len(old)} -> {len(new)}"
    pd.testing.assert_frame_equal(old, new, check_dtype=False, obj=label)


def snapshot(con):
    return {
        "features": frame(con, "SELECT * FROM player_features"),
        "projections": frame(con, "SELECT * FROM player_projections"),
        "market": frame(con, "SELECT * FROM market_lines"),
    }


def assert_nothing_before_changed(old, new, t, *, include_t=True):
    """Every pre-game value at week_index <= t (or < t) is identical."""
    cmp = (lambda s: s <= t) if include_t else (lambda s: s < t)
    feat_cols = [c for c in old["features"].columns
                 if c not in FEATURE_KEYS + YARD_COLS + ["receptions"]]
    of, nf = old["features"], new["features"]
    assert_same(of[cmp(wi(of))], nf[cmp(wi(nf))], FEATURE_KEYS, feat_cols, "player_features")
    op, np_ = old["projections"], new["projections"]
    assert_same(op[cmp(op.week_index)], np_[cmp(np_.week_index)], PROJ_KEYS, PROJ_PRE_GAME,
                "player_projections")
    om, nm = old["market"], new["market"]
    assert_same(om[cmp(wi(om))], nm[cmp(wi(nm))], ["odds_id"], ML_PRE_GAME[1:],
                "market_lines")


# --------------------------------------------------------------------------- 1. leakage
@pytest.mark.parametrize("season,week", [(2022, 1), (2022, 4), (2023, 1), (2023, 3),
                                         (2023, 4), (2023, 5), (2023, 6)])
def test_mutating_every_stat_of_every_player_in_week_t_changes_nothing_at_or_before_t(
    stats_frame, tmp_path, season, week
):
    t = season * 100 + week
    base = run(stats_frame, tmp_path / "base")
    bumped = stats_frame.copy()
    hit = (bumped.season == season) & (bumped.week == week)
    assert hit.sum() >= 8
    for col in YARD_COLS:
        bumped.loc[hit, col] = bumped.loc[hit, col].fillna(0) * 3 + 137.0
    for col in COUNT_COLS:
        bumped.loc[hit, col] = bumped.loc[hit, col] + 7
    new = run(bumped, tmp_path / "bumped")
    old_s, new_s = snapshot(base), snapshot(new)

    assert_nothing_before_changed(old_s, new_s, t, include_t=True)

    # Week-t actuals DID change (the mutation took effect) ...
    of, nf = old_s["features"], new_s["features"]
    assert (nf.loc[wi(nf) == t, "receptions"].to_numpy()
            != of.loc[wi(of) == t, "receptions"].to_numpy()).all()
    # ... and later pre-game values see it (unless t is the last week).
    if t != 202306:
        op, np_ = old_s["projections"], new_s["projections"]
        later = lambda p: p[p.week_index > t].sort_values(PROJ_KEYS)["sd_prior"].to_numpy()
        assert not np.allclose(later(op), later(np_), equal_nan=True)
    base.close()
    new.close()


def test_mutating_and_extending_future_seasons_changes_nothing_in_earlier_seasons(
    stats_frame, tmp_path
):
    base = run(stats_frame, tmp_path / "base")
    future = stats_frame.copy()
    # wreck every 2023 stat
    hit = future.season == 2023
    for col in STAT_COLS:
        future.loc[hit, col] = future.loc[hit, col].fillna(0) * 5 + 99
    # and append a whole fake 2024 season (all players, new teams)
    extra = stats_frame[stats_frame.season == 2023].copy()
    extra["season"] = extra["season"].astype("int32") + 1
    extra["recent_team"] = "NYG"
    for col in STAT_COLS:
        extra[col] = extra[col].fillna(0) * 10 + 1
    future = pd.concat([future, extra], ignore_index=True)
    future["season"] = future["season"].astype("int32")
    new = run(future, tmp_path / "future")
    assert_nothing_before_changed(snapshot(base), snapshot(new), 202299, include_t=True)
    base.close()
    new.close()


def test_appending_future_season_changes_no_2023_pre_game_values(stats_frame, tmp_path):
    base = run(stats_frame, tmp_path / "base")
    extra = stats_frame[stats_frame.season == 2023].copy()
    extra["season"] = (extra["season"] + 1).astype("int32")
    extra["recent_team"] = "NYG"
    new = run(pd.concat([stats_frame, extra], ignore_index=True), tmp_path / "plus2024")
    assert_nothing_before_changed(snapshot(base), snapshot(new), 202399, include_t=True)
    base.close()
    new.close()


def test_future_roster_move_does_not_change_earlier_market_lines(stats_frame, tmp_path):
    """Leakage through identity, not stats: a second 'DJ Moore' who only joins
    CHI in 2023 week 5 must not change how the week-4 'D.J. Moore, CHI' line
    (odds_id 1) was matched / priced. Only rows at weeks >= 5 are added."""
    base = run(stats_frame, tmp_path / "base")
    newcomer = clone_player(stats_frame, DJ_MOORE, "00-0000099", team="CHI",
                            keep=lambda r: (r.season == 2023) & (r.week >= 5))
    assert len(newcomer) == 2
    new = run(pd.concat([stats_frame, newcomer], ignore_index=True), tmp_path / "newcomer")
    old_s, new_s = snapshot(base), snapshot(new)
    om, nm = old_s["market"], new_s["market"]
    assert_same(om[wi(om) <= 202304], nm[wi(nm) <= 202304], ["odds_id"], ML_PRE_GAME[1:],
                "market_lines (weeks <= 2023w4)")
    base.close()
    new.close()


# --------------------------------------------------------------------------- 2. duplicates
@pytest.mark.parametrize("who,season,week", [
    (KELCE, 2023, 5),        # a player-week that an odds row points at
    ("00-0000007", 2022, 1),  # a player-week no odds row touches
])
def test_exact_duplicate_raw_row_fails_with_clear_error(stats_frame, tmp_path, who, season, week):
    dup = stats_frame[(stats_frame.player_id == who) & (stats_frame.season == season)
                      & (stats_frame.week == week)]
    assert len(dup) == 1
    df = pd.concat([stats_frame, dup], ignore_index=True)
    with pytest.raises(AssertionError, match=r"stg_player_week_dupes: 1\b"):
        run(df, tmp_path)


def test_conflicting_duplicate_raw_row_fails_with_clear_error(stats_frame, tmp_path):
    dup = stats_frame[(stats_frame.player_id == DJ_MOORE) & (stats_frame.season == 2023)
                      & (stats_frame.week == 2)].copy()
    dup["receiving_yards"] = 999.0
    dup["recent_team"] = "NYG"  # different team too -> extra xwalk key
    df = pd.concat([stats_frame, dup], ignore_index=True)
    with pytest.raises(AssertionError, match="stg_player_week_dupes"):
        run(df, tmp_path)


def test_duplicate_odds_id_fails_checks(stats_frame, tmp_path):
    """market_lines' grain is odds_id; a CSV that repeats an odds_id must not
    silently produce two bets for one id (the fan-out check compares counts only)."""
    line = dict(odds_id=1, season=2023, week=4, player_name="DJ Moore", team="CHI",
                market="rec_yds", open_line=70.5)
    odds = write_odds([line, dict(line)], tmp_path / "odds")
    try:
        con = run(stats_frame, tmp_path / "stats", odds)
    except AssertionError:
        return  # rejected by run_checks: good
    dupes = con.execute("""SELECT count(*) - count(DISTINCT odds_id) FROM market_lines
                           UNION ALL
                           SELECT count(*) - count(DISTINCT odds_id) FROM bet_results""").fetchall()
    con.close()
    assert dupes == [(0,), (0,)], f"duplicate odds_id passed run_checks: extra rows {dupes}"


# --------------------------------------------------------------------------- 3. edge cases
def _with_rookie(stats_frame):
    """'Rookie Guy' WR NYG: 2023 weeks 1-4 only (one season, exactly 4 games)."""
    rookie = clone_player(stats_frame, DJ_MOORE, "00-0000077", name="Rookie Guy", team="NYG",
                          keep=lambda r: (r.season == 2023) & (r.week <= 4))
    return pd.concat([stats_frame, rookie], ignore_index=True)


def test_exactly_three_prior_games_gets_a_projection_two_does_not(stats_frame, tmp_path):
    odds = write_odds([
        dict(odds_id=1, season=2023, week=3, player_name="Rookie Guy", team="NYG",
             market="rec_yds", open_line=70.5),
        dict(odds_id=2, season=2023, week=4, player_name="Rookie Guy", team="NYG",
             market="rec_yds", open_line=70.5),
    ], tmp_path / "odds")
    con = run(_with_rookie(stats_frame), tmp_path / "stats", odds)
    p = frame(con, """SELECT week, n_prior_games, projection FROM player_projections
                      WHERE player_id = '00-0000077' AND market = 'rec_yds' ORDER BY week""")
    assert p.n_prior_games.tolist() == [0, 1, 2, 3]
    assert p.projection.isna().tolist() == [True, True, True, False]
    expected = np.mean([75 + 5 * ((w + 2023) % 4) for w in (1, 2, 3)])
    assert p.projection.iloc[3] == pytest.approx(expected)
    status = dict(con.execute("SELECT odds_id, match_status FROM market_lines").fetchall())
    assert status == {1: "no_projection", 2: "matched"}
    con.close()


def test_single_season_player_has_no_history_and_no_other_season_key(stats_frame, tmp_path):
    odds = write_odds([
        dict(odds_id=1, season=2022, week=5, player_name="Rookie Guy", team="NYG",
             market="rec_yds", open_line=70.5),
        dict(odds_id=2, season=2023, week=1, player_name="Rookie Guy", team="NYG",
             market="rec_yds", open_line=70.5),
    ], tmp_path / "odds")
    con = run(_with_rookie(stats_frame), tmp_path / "stats", odds)
    [(seq, n, avg)] = con.execute("""SELECT game_seq, n_prior_games, avg_rec_yds_l6
        FROM player_features WHERE player_id = '00-0000077' AND season = 2023 AND week = 1
    """).fetchall()
    assert (seq, n, avg) == (1, 0, None)
    got = {r[0]: r[1:] for r in con.execute(
        "SELECT odds_id, match_status, player_id FROM market_lines").fetchall()}
    assert got[1] == ("no_player", None)          # no 2022 key for him
    assert got[2] == ("no_projection", "00-0000077")
    con.close()


def test_unknown_team_alias_gives_question_mark_key_and_no_player(stats_frame, tmp_path):
    odds = write_odds([
        dict(odds_id=1, season=2023, week=4, player_name="D.J. Moore", team="Chicago Cubs",
             market="rec_yds", open_line=70.5),
        dict(odds_id=2, season=2023, week=4, player_name="D.J. Moore", team="",
             market="rec_yds", open_line=70.5),
    ], tmp_path / "odds")
    con = run(stats_frame, tmp_path / "stats", odds)
    got = {r[0]: r[1:] for r in con.execute(
        "SELECT odds_id, team, join_key, match_status, player_id FROM market_lines").fetchall()}
    assert got[1] == (None, "djmoore|?", "no_player", None)
    assert got[2] == (None, "djmoore|?", "no_player", None)
    con.close()


def test_unknown_team_on_both_sides_is_never_matched(stats_frame, tmp_path):
    """'?' is "team unknown", not a team: an unrecognised stats team and an
    unrecognised odds team must not be treated as the same team."""
    df = stats_frame.copy()
    df.loc[df.player_id == "00-0000007", "recent_team"] = "XXX"  # Engram: junk team code
    odds = write_odds([
        dict(odds_id=1, season=2023, week=6, player_name="Evan Engram", team="Jaguarz",
             market="rec_yds", open_line=44.5),
    ], tmp_path / "odds")
    con = run(df, tmp_path / "stats", odds)
    [(key, status)] = con.execute("SELECT join_key, match_status FROM market_lines").fetchall()
    assert key == "evanengram|?"
    assert status == "no_player"
    con.close()


def test_odds_week_with_no_stats_at_all(stats_frame, tmp_path):
    odds = write_odds([
        # season exists, week 9 has no stats rows for anyone
        dict(odds_id=1, season=2023, week=9, player_name="DJ Moore", team="CHI",
             market="rec_yds", open_line=70.5),
        # season with no stats at all
        dict(odds_id=2, season=2024, week=1, player_name="DJ Moore", team="CHI",
             market="rec_yds", open_line=70.5),
    ], tmp_path / "odds")
    con = run(stats_frame, tmp_path / "stats", odds)
    got = {r[0]: r[1:] for r in con.execute(
        "SELECT odds_id, match_status, player_id, projection, p_over_model FROM market_lines"
    ).fetchall()}
    assert got[1] == ("no_projection", DJ_MOORE, None, None)
    assert got[2] == ("no_player", None, None, None)
    assert con.execute("SELECT count(*) FROM bet_results").fetchall() == [(0,)]
    con.close()


@pytest.mark.parametrize("price,prob,profit", [
    (-110, 110 / 210, 100 / 110),
    (-250, 250 / 350, 0.4),
    (-100, 0.5, 1.0),
    (100, 0.5, 1.0),
    (150, 0.4, 1.5),
    (250, 100 / 350, 2.5),
    (-10000, 10000 / 10100, 0.01),
    (10000, 100 / 10100, 100.0),
])
def test_american_price_conversions(con, price, prob, profit):
    [(p, u)] = con.execute(
        f"SELECT american_to_prob({price}::INTEGER), american_profit({price}::INTEGER)"
    ).fetchall()
    assert p == pytest.approx(prob)
    assert u == pytest.approx(profit)


def test_positive_and_negative_prices_through_bet_results(stats_frame, tmp_path):
    # DJ Moore 2023 wk5 rec_yds actual = 75 + 5*((5+2023)%4) = 75. Lines far below
    # -> model and result both say over; graded at the OVER open price.
    odds = write_odds([
        dict(odds_id=1, season=2023, week=5, player_name="DJ Moore", team="CHI",
             market="rec_yds", open_line=10.5, open_over_price=240, open_under_price=-300),
        dict(odds_id=2, season=2023, week=5, player_name="DJ Moore", team="CHI",
             market="rec_yds", open_line=10.5, open_over_price=-180, open_under_price=150),
    ], tmp_path / "odds")
    con = run(stats_frame, tmp_path / "stats", odds)
    got = {r[0]: r[1:] for r in con.execute("""SELECT odds_id, side, result, side_open_price,
        profit_units, nv_open_over FROM bet_results""").fetchall()}
    assert got[1][:4] == ("over", "over", 240, pytest.approx(2.4))
    assert got[2][:4] == ("over", "over", -180, pytest.approx(100 / 180))
    imp_o, imp_u = 100 / 340, 300 / 400
    assert got[1][4] == pytest.approx(imp_o / (imp_o + imp_u))
    con.close()


def test_extreme_model_probabilities_bucket_correctly(stats_frame, tmp_path):
    lines = [0.5, 10.5, 60.5, 70.5, 75.5, 80.5, 90.5, 500.5, 5000.5]
    odds = write_odds([
        dict(odds_id=i + 1, season=2023, week=5, player_name="DJ Moore", team="CHI",
             market="rec_yds", open_line=line)
        for i, line in enumerate(lines)
    ], tmp_path / "odds")
    con = run(stats_frame, tmp_path / "stats", odds)
    b = frame(con, "SELECT odds_id, p_over_model, result FROM bet_results ORDER BY odds_id")
    assert len(b) == len(lines)
    assert b.p_over_model.between(0.0, 1.0).all()
    assert b.p_over_model.iloc[0] > 0.999 and b.p_over_model.iloc[-1] < 1e-6
    cal = frame(con, "SELECT * FROM calibration_buckets ORDER BY bucket_lo")
    valid = {round(i / 10, 1) for i in range(10)}
    assert set(cal.bucket_lo.round(10)) <= valid
    assert (cal.bucket_hi <= 1.0 + 1e-12).all()
    np.testing.assert_allclose(cal.bucket_hi, cal.bucket_lo + 0.1)
    assert cal.n_bets.sum() == (b.result != "push").sum()
    # p ~ 1 lands in the top bucket, p ~ 0 in the bottom one
    assert cal.bucket_lo.iloc[-1] == pytest.approx(0.9)
    assert cal.bucket_lo.iloc[0] == pytest.approx(0.0)
    # each bet is in the bucket containing its probability
    for p in b.loc[b.result != "push", "p_over_model"]:
        lo = min(np.floor(p * 10) / 10, 0.9)
        assert lo in set(cal.bucket_lo.round(10)), p
    con.close()


def test_whole_number_line_push_on_an_under_bet(stats_frame, tmp_path):
    # Kelce 2023 wk5 receptions actual = 5 + (5+2023)%3 = 5. Market prices the
    # over at -100000 so the fair P(over) ~ 0.998 > model -> the model takes the UNDER.
    odds = write_odds([
        dict(odds_id=1, season=2023, week=5, player_name="Travis Kelce", team="KC",
             market="receptions", open_line=5.0, open_over_price=-100000,
             open_under_price=50000, close_line=5.0, close_over_price=-100000,
             close_under_price=50000),
        dict(odds_id=2, season=2023, week=5, player_name="Travis Kelce", team="KC",
             market="receptions", open_line=4.5, open_over_price=-110,
             open_under_price=-110, close_line=4.5),
    ], tmp_path / "odds")
    con = run(stats_frame, tmp_path / "stats", odds)
    [(side, actual, result, won, profit, beat)] = con.execute("""
        SELECT side, actual, result, side_won, profit_units, beat_close
        FROM bet_results WHERE odds_id = 1""").fetchall()
    assert side == "under"
    assert (actual, result, won, profit) == (5.0, "push", None, 0.0)
    assert beat is False
    assert con.execute("SELECT sum(n_bets) FROM calibration_buckets").fetchall() == [(1,)]
    [(n, pushes, total, roi)] = con.execute("""SELECT sum(n_bets), sum(n_pushes),
        sum(total_profit_units), sum(roi) FROM clv_summary""").fetchall()
    assert (n, pushes) == (2, 1)
    [(other_profit,)] = con.execute(
        "SELECT profit_units FROM bet_results WHERE odds_id = 2").fetchall()
    assert total == pytest.approx(other_profit)
    assert roi == pytest.approx(other_profit / 1)  # push risks nothing
    con.close()


def test_missing_open_line_is_not_graded_as_a_push(stats_frame, tmp_path):
    """A line with no open_line has no model probability and nothing to grade
    against; it must not become a 'matched' bet graded 'push'."""
    odds = write_odds([
        dict(odds_id=1, season=2023, week=5, player_name="DJ Moore", team="CHI",
             market="rec_yds", open_line="", close_line=70.5),
    ], tmp_path / "odds")
    con = run(stats_frame, tmp_path / "stats", odds)
    [(status, p)] = con.execute("SELECT match_status, p_over_model FROM market_lines").fetchall()
    assert p is None
    graded = con.execute("SELECT result, profit_units FROM bet_results").fetchall()
    assert status != "matched" and graded == [], (status, graded)
    con.close()


# --------------------------------------------------------------------------- 4. row counts
def test_row_counts_match_plan_with_trade_and_name_collision_same_week(stats_frame, tmp_path):
    df = stats_frame.copy()
    # A third "Mike Williams": NYJ through 2023 week 3, traded to LAC in week 4 --
    # the same week Marquise Brown moves ARI -> KC. LAC then has 3 Mike Williamses.
    mw3 = clone_player(df, "00-0000009", "00-0000011", team="NYJ")
    mw3.loc[(mw3.season == 2023) & (mw3.week >= 4), "recent_team"] = "LAC"
    # A "Travis Kelce" on a different team the same weeks: same name, NOT ambiguous.
    tk2 = clone_player(df, KELCE, "00-0000012", team="CIN",
                       keep=lambda r: r.season == 2023)
    df = pd.concat([df, mw3, tk2], ignore_index=True)
    odds_rows = [
        dict(odds_id=1, season=2023, week=4, player_name="Mike Williams", team="LAC",
             market="rec_yds", open_line=50.5),   # ambiguous (3 players)
        dict(odds_id=2, season=2023, week=2, player_name="Mike Williams", team="NYJ",
             market="rec_yds", open_line=50.5),   # unique: 00-0000011
        dict(odds_id=3, season=2023, week=4, player_name="Hollywood Brown", team="KC",
             market="receptions", open_line=5.5),
        dict(odds_id=4, season=2023, week=4, player_name="Marquise Brown", team="ARI",
             market="receptions", open_line=5.5),  # stale team
        dict(odds_id=5, season=2023, week=4, player_name="Travis Kelce", team="KC",
             market="receptions", open_line=5.5),
        dict(odds_id=6, season=2023, week=4, player_name="Travis Kelce", team="CIN",
             market="receptions", open_line=5.5),
        dict(odds_id=7, season=2023, week=4, player_name="Travis Kelce", team="KC",
             market="rec_yds", open_line=60.5),
    ]
    odds = write_odds(odds_rows, tmp_path / "odds")
    con = run(df, tmp_path / "stats", odds)

    def n(table, where="TRUE"):
        return con.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchall()[0][0]

    skill = df[(df.season_type == "REG") & df.position.isin(["QB", "RB", "WR", "TE"])]
    assert n("stg_player_week") == len(skill)
    assert n("player_features") == len(skill)
    xw = skill.assign(team=skill.recent_team)[["player_id", "season", "team"]].drop_duplicates()
    assert n("player_name_xwalk") == len(xw)
    per_pos = skill.position.value_counts().to_dict()
    eligible = {"pass_yds": ["QB"], "rush_yds": ["QB", "RB"],
                "rec_yds": ["RB", "WR", "TE"], "receptions": ["RB", "WR", "TE"]}
    for market, pos in eligible.items():
        assert n("player_projections", f"market = '{market}'") == sum(
            per_pos.get(p, 0) for p in pos), market
    assert n("stg_odds") == len(odds_rows)
    assert con.execute("SELECT count(*), count(DISTINCT odds_id) FROM market_lines"
                       ).fetchall() == [(len(odds_rows), len(odds_rows))]
    assert n("bet_results") == n("market_lines", "match_status = 'matched'")
    assert con.execute("SELECT sum(n_bets) FROM clv_summary").fetchall()[0][0] == n("bet_results")
    assert con.execute("SELECT sum(n_bets) FROM calibration_buckets").fetchall()[0][0] == n(
        "bet_results", "result <> 'push'")

    got = {r[0]: r[1:] for r in con.execute(
        "SELECT odds_id, match_status, player_id FROM market_lines").fetchall()}
    assert got[1] == ("ambiguous_name", None)
    assert got[2] == ("matched", "00-0000011")
    assert got[3] == ("matched", "00-0000004")
    assert got[4][1] == "00-0000004"
    assert got[5] == ("matched", KELCE)
    assert got[6] == ("matched", "00-0000012")
    amb = frame(con, """SELECT DISTINCT join_key FROM player_name_xwalk
                        WHERE season = 2023 AND is_ambiguous""")
    assert set(amb.join_key) == {"mikewilliams|LAC"}
    con.close()
