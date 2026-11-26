"""Tests for sql/03_market.sql and sql/04_eval.sql, on the FAKE odds fixture."""

import csv

import pytest


def rows(con, sql):
    return con.execute(sql).fetchall()


def test_odds_fixture_is_labeled_fake(odds_csv):
    assert "FAKE" in odds_csv.name
    with open(odds_csv, newline="") as fh:
        sources = {row["source"] for row in csv.DictReader(fh)}
    assert sources == {"FAKE"}


def test_stg_odds_row_count_matches_csv(con, odds_csv):
    with open(odds_csv, newline="") as fh:
        n_csv = sum(1 for _ in csv.DictReader(fh))
    assert n_csv == 12
    assert rows(con, "SELECT count(*) FROM stg_odds") == [(n_csv,)]


def test_market_join_has_no_fanout(con):
    assert rows(con, """
        SELECT count(*), count(DISTINCT odds_id) FROM market_lines
    """) == [(12, 12)]
    assert rows(con, """
        SELECT count(*) FROM stg_odds AS s
        ANTI JOIN market_lines AS m USING (odds_id)
    """) == [(0,)]


def test_match_status_counts(con):
    got = dict(rows(con, "SELECT match_status, count(*) FROM market_lines GROUP BY ALL"))
    assert got == {"matched": 9, "ambiguous_name": 1, "no_player": 1, "no_game": 1}
    status = dict(rows(con, "SELECT odds_id, match_status FROM market_lines"))
    assert status[10] == "ambiguous_name"
    assert status[11] == "no_player"
    assert status[12] == "no_game"  # Kelce has no game in week 7


def test_aliases_resolve_to_expected_players(con):
    got = dict(rows(con, "SELECT odds_id, player_id FROM market_lines"))
    expected = {
        1: "00-0000003",  # D.J. Moore -> DJ Moore
        3: "00-0000004",  # Hollywood Brown, ARZ -> Marquise Brown, ARI
        4: "00-0000004",  # ... after the trade to KC
        5: "00-0000005",  # team given as "Kansas City Chiefs"
        6: "00-0000006",  # Ken Walker -> Kenneth Walker III
        8: "00-0000007",  # JAC -> JAX
    }
    for odds_id, player_id in expected.items():
        assert got[odds_id] == player_id, odds_id
    assert got[10] is None  # ambiguous: never guessed


def test_american_to_prob_known_values(con):
    [(a, b, c)] = rows(con, """
        SELECT american_to_prob(-110), american_to_prob(150), american_to_prob(100)
    """)
    assert a == pytest.approx(0.52381, abs=1e-5)
    assert b == pytest.approx(0.4)
    assert c == pytest.approx(0.5)


def test_no_vig_probs_sum_to_one(con):
    for open_sum, close_sum in rows(con, """
        SELECT nv_open_over + nv_open_under, nv_close_over + nv_close_under FROM stg_odds
    """):
        assert open_sum == pytest.approx(1.0)
        assert close_sum == pytest.approx(1.0)


def test_bet_results_row_count_equals_matched(con):
    assert rows(con, "SELECT count(*) FROM bet_results") == [(9,)]


def test_push_on_whole_number_line(con):
    # Kelce receptions line 5.0, actual 5 -> push, side_won NULL, profit 0
    assert rows(con, """
        SELECT actual, result, side_won, profit_units FROM bet_results WHERE odds_id = 5
    """) == [(5.0, "push", None, 0.0)]
    assert rows(con, "SELECT count(*) FROM bet_results WHERE result = 'push'") == [(1,)]


def test_clv_sign_convention(con):
    b = {r[0]: r[1:] for r in rows(con, """
        SELECT odds_id, side, open_line, close_line, clv_line_pts, clv_prob,
               side_open_prob, side_close_prob, beat_close
        FROM bet_results""")}
    for odds_id, (side, open_, close, pts, prob, p_open, p_close, beat) in b.items():
        expected_pts = close - open_ if side == "over" else open_ - close
        assert pts == pytest.approx(expected_pts), odds_id
        if open_ == close:
            assert prob == pytest.approx(p_close - p_open), odds_id
        else:
            assert prob is None, odds_id  # not comparable across different lines
        assert beat == (pts > 0 or (pts == 0 and prob is not None and prob > 0)), odds_id
    # odds 1: over, line 70.5 -> 72.5 = +2 points of CLV
    assert b[1][0] == "over" and b[1][3] == pytest.approx(2.0) and b[1][7] is True
    # odds 7: same line, over got pricier (-115 -> -125) -> positive prob CLV
    assert b[7][0] == "over" and b[7][4] > 0 and b[7][7] is True


def test_profit_units(con):
    got = dict(rows(con, "SELECT odds_id, profit_units FROM bet_results"))
    for odds_id, side_won, price, profit in rows(con, """
        SELECT odds_id, side_won, side_open_price, profit_units FROM bet_results"""):
        if side_won is None:
            assert profit == 0.0
        elif not side_won:
            assert profit == -1.0
        elif price > 0:
            assert profit == pytest.approx(price / 100)
        else:
            assert profit == pytest.approx(100 / -price)
    assert got[1] == pytest.approx(100 / 110)  # won the over at -110
    assert got[2] == pytest.approx(1.0)        # won the over at +100


def test_calibration_counts_match_non_push_bets(con):
    assert rows(con, "SELECT sum(n_bets) FROM calibration_buckets") == [(8,)]
    for lo, hi in rows(con, "SELECT bucket_lo, bucket_hi FROM calibration_buckets"):
        assert lo in {round(i / 10, 1) for i in range(10)}
        assert hi == pytest.approx(lo + 0.1)


def test_invalid_american_prices_give_null(con):
    # prices strictly between -100 and +100 don't exist in American odds
    assert rows(con, """
        SELECT american_to_prob(0), american_to_prob(-50), american_profit(99),
               american_to_prob(-100), american_profit(100)
    """) == [(None, None, None, 0.5, 1.0)]


def _odds_file(tmp_path, lines):
    header = ("source,odds_id,book,season,week,player_name,team,market,open_line,"
              "open_over_price,open_under_price,close_line,close_over_price,close_under_price")
    path = tmp_path / "FAKE_small_odds.csv"
    path.write_text("\n".join([header, *lines]) + "\n")
    return path


def test_bad_odds_stale_team_and_unknown_close(stats_frame, tmp_path):
    from src.pipeline import run_pipeline

    stats = tmp_path / "stats.parquet"
    stats_frame.to_parquet(stats, index=False)
    odds = _odds_file(tmp_path, [
        "FAKE,1,b,2023,5,DJ Moore,CHI,rec_yds,70.5,50,-110,70.5,-110,-110",   # invalid price
        "FAKE,2,b,2023,5,DJ Moore,CHI,rec_yds,,-110,-110,70.5,-110,-110",     # no open line
        "FAKE,3,b,2023,5,Marquise Brown,ARI,rec_yds,50.5,-110,-110,50.5,-110,-110",  # stale team
        "FAKE,4,b,2023,5,DJ Moore,CHI,rec_yds,70.5,-110,-110,,,",             # close unknown
    ])
    c = run_pipeline(stats_path=stats, odds_path=odds)
    status = dict(rows(c, "SELECT odds_id, match_status FROM market_lines"))
    assert status == {1: "bad_odds", 2: "bad_odds", 3: "no_game", 4: "matched"}
    [(beat, clv_prob, result)] = rows(c, "SELECT beat_close, clv_prob, result FROM bet_results")
    assert beat is None and clv_prob is None and result is not None
    [(pct,)] = rows(c, "SELECT pct_beat_close FROM clv_summary")
    assert pct is None  # the only bet has an unknown close: ignored, not counted as 0
    c.close()
