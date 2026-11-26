"""Tests for sql/01_staging.sql: stg_player_week and player_name_xwalk."""

from src.resolve import build_key


def rows(con, sql):
    return con.execute(sql).fetchall()


def test_stg_one_row_per_player_week(con):
    assert rows(con, """
        SELECT count(*) FROM (
            SELECT player_id, season, week FROM stg_player_week
            GROUP BY ALL HAVING count(*) > 1)
    """) == [(0,)]


def test_stg_row_count_is_101(con):
    # 8 skill players x 12 games - 1 Mahomes bye + 6 games of the 2nd Mike Williams
    assert rows(con, "SELECT count(*) FROM stg_player_week") == [(101,)]


def test_stg_excludes_post_and_non_skill_positions(con):
    assert rows(con, "SELECT count(*) FROM stg_player_week WHERE week > 18") == [(0,)]
    positions = {p for (p,) in rows(con, "SELECT DISTINCT position FROM stg_player_week")}
    assert positions == {"QB", "RB", "WR", "TE"}


def test_stg_nulls_become_zero(con):
    # Kelce 2022 week 2 had receiving_yards = NaN in the raw data
    assert rows(con, """
        SELECT receiving_yards FROM stg_player_week
        WHERE player_id = '00-0000005' AND season = 2022 AND week = 2
    """) == [(0.0,)]
    assert rows(con, """
        SELECT count(*) FROM stg_player_week
        WHERE passing_yards IS NULL OR rushing_yards IS NULL OR receiving_yards IS NULL
           OR attempts IS NULL OR completions IS NULL OR carries IS NULL
           OR targets IS NULL OR receptions IS NULL
    """) == [(0,)]


def test_xwalk_join_key_matches_python_build_key(con, stats_frame):
    expected = {
        (r.player_id, int(r.season), build_key(r.player_display_name, r.recent_team))
        for r in stats_frame.itertuples()
        if r.season_type == "REG" and r.position in ("QB", "RB", "WR", "TE")
    }
    actual = set(rows(con, "SELECT player_id, season, join_key FROM player_name_xwalk"))
    assert actual == expected


def test_xwalk_traded_player_has_row_per_team(con):
    got = rows(con, """
        SELECT team, join_key, first_week, last_week, n_games
        FROM player_name_xwalk
        WHERE player_id = '00-0000004' AND season = 2023
        ORDER BY first_week
    """)
    assert got == [
        ("ARI", "marquisebrown|ARI", 1, 3, 3),
        ("KC", "marquisebrown|KC", 4, 6, 3),
    ]


def test_xwalk_flags_ambiguous_names(con):
    got = dict(rows(con, """
        SELECT season, bool_and(is_ambiguous)
        FROM player_name_xwalk
        WHERE join_key = 'mikewilliams|LAC'
        GROUP BY season
    """))
    assert got == {2022: False, 2023: True}
    # nobody else is ambiguous
    assert rows(con, """
        SELECT count(*) FROM player_name_xwalk
        WHERE is_ambiguous AND join_key <> 'mikewilliams|LAC'
    """) == [(0,)]
