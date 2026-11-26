-- =============================================================================
-- 01_staging.sql
--
-- Builds:
--   stg_player_week    one row per (player_id, season, week)
--                      regular-season games of QB/RB/WR/TE, cleaned and typed
--   player_name_xwalk  one row per (player_id, season, join_key)
--                      the "which player does this name+team mean?" lookup
--
-- Input:  raw_player_stats  (view over the nflverse parquet files, made by
--                            src/pipeline.py)
-- UDFs:   normalize_team, canonical_name, build_key  (Python, src/resolve.py)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- stg_player_week
-- Grain: player_id, season, week
--
-- No dedupe here on purpose: if the source ever has two rows for the same
-- player-week we want that to FAIL LOUDLY (pipeline checks + tests), not be
-- silently hidden by a DISTINCT or a row_number() = 1 filter.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE stg_player_week AS
WITH
-- Regular season only: playoff games never enter the rolling windows.
-- Skill positions only: those are the only ones we have prop markets for.
reg_season_skill AS (
    SELECT
        player_id,
        player_display_name,
        position,
        recent_team,
        opponent_team,
        season,
        week,
        attempts,
        completions,
        carries,
        targets,
        receptions,
        passing_yards,
        rushing_yards,
        receiving_yards
    FROM raw_player_stats
    WHERE season_type = 'REG'
      AND position IN ('QB', 'RB', 'WR', 'TE')
),

-- Fix types, turn missing stats into 0, and attach the resolution keys.
-- coalesce() is defensive: the real data has no NULL stats today.
cleaned AS (
    SELECT
        player_id,
        player_display_name,
        position,
        normalize_team(recent_team)                     AS team,
        normalize_team(opponent_team)                   AS opponent,
        CAST(season AS INTEGER)                         AS season,
        CAST(week AS INTEGER)                           AS week,
        -- season*100 + week sorts chronologically across seasons (202318 < 202401)
        CAST(season AS INTEGER) * 100 + CAST(week AS INTEGER) AS week_index,
        CAST(coalesce(attempts, 0)        AS INTEGER)   AS attempts,
        CAST(coalesce(completions, 0)     AS INTEGER)   AS completions,
        CAST(coalesce(carries, 0)         AS INTEGER)   AS carries,
        CAST(coalesce(targets, 0)         AS INTEGER)   AS targets,
        CAST(coalesce(receptions, 0)      AS INTEGER)   AS receptions,
        CAST(coalesce(passing_yards, 0)   AS DOUBLE)    AS passing_yards,
        CAST(coalesce(rushing_yards, 0)   AS DOUBLE)    AS rushing_yards,
        CAST(coalesce(receiving_yards, 0) AS DOUBLE)    AS receiving_yards,
        canonical_name(player_display_name)             AS name_key,
        -- join_key must equal src/resolve.build_key(name, team) exactly,
        -- because the odds side is keyed the same way.
        build_key(player_display_name, recent_team)     AS join_key
    FROM reg_season_skill
)

SELECT
    player_id,
    player_display_name,
    position,
    team,
    opponent,
    season,
    week,
    week_index,
    attempts,
    completions,
    carries,
    targets,
    receptions,
    passing_yards,
    rushing_yards,
    receiving_yards,
    name_key,
    join_key
FROM cleaned
ORDER BY player_id, season, week;


-- -----------------------------------------------------------------------------
-- player_name_xwalk
-- Grain: player_id, season, join_key
--
-- A traded player gets one row per team he played for that season
-- (e.g. marquisebrown|ARI and marquisebrown|KC), so a sportsbook line is
-- matched to the team he was on at game time.
--
-- is_ambiguous: more than one distinct player_id shares the same
-- (season, join_key) - e.g. two "Mike Williams" on LAC. Ambiguous keys are
-- NEVER matched downstream; we refuse to guess.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE player_name_xwalk AS
WITH
-- One row per player, season and team stint.
player_team_seasons AS (
    SELECT
        player_id,
        season,
        team,
        any_value(player_display_name) AS player_display_name,
        name_key,
        join_key,
        min(week)                      AS first_week,
        max(week)                      AS last_week,
        count(*)                       AS n_games
    FROM stg_player_week
    GROUP BY player_id, season, team, name_key, join_key
),

-- How many different players use each key in a season?
-- (DuckDB can do count(DISTINCT ...) OVER (...), but a plain GROUP BY joined
-- back is easier to read and to check by hand.)
key_counts AS (
    SELECT
        season,
        join_key,
        count(DISTINCT player_id) AS n_players
    FROM player_team_seasons
    GROUP BY season, join_key
)

SELECT
    p.player_id,
    p.season,
    p.team,
    p.player_display_name,
    p.name_key,
    p.join_key,
    p.first_week,
    p.last_week,
    p.n_games,
    k.n_players > 1 AS is_ambiguous
FROM player_team_seasons AS p
JOIN key_counts AS k
  ON k.season = p.season
 AND k.join_key = p.join_key
ORDER BY p.season, p.join_key, p.player_id;
