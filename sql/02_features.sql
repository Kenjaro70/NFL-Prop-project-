-- =============================================================================
-- 02_features.sql
--
-- Builds:
--   player_features     one row per stg_player_week row (player_id, season, week)
--                       rolling averages of the player's PREVIOUS games
--   player_projections  one row per (player_id, season, week, market)
--                       projection, actual result, and a model standard deviation
--
-- Input:  stg_player_week (01_staging.sql)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- player_features
-- Grain: player_id, season, week  (same rows as stg_player_week)
--
-- The window "last6" is the heart of the no-leakage rule:
--   * PARTITION BY player_id ORDER BY season, week  -> CROSSES SEASONS.
--     A within-season window would leave weeks 1-3 of every season (~17% of
--     the sample, often the softest lines) without a projection. The cost is
--     that offseason role/team changes carry stale history into September.
--   * ROWS (not RANGE) counts GAMES, not weeks: byes and missed games are
--     simply skipped, so "last 6" = the player's last 6 recorded games.
--   * 6 PRECEDING AND 1 PRECEDING ends BEFORE the current row, so a game's
--     features never see that game's own stats. On a player's first game the
--     frame is empty -> averages are NULL and n_prior_games = 0.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE player_features AS
SELECT
    player_id,
    season,
    week,
    week_index,
    position,
    team,
    row_number() OVER (PARTITION BY player_id ORDER BY season, week) AS game_seq,
    count(*)             OVER last6 AS n_prior_games,
    avg(passing_yards)   OVER last6 AS avg_pass_yds_l6,
    avg(rushing_yards)   OVER last6 AS avg_rush_yds_l6,
    avg(carries)         OVER last6 AS avg_carries_l6,
    avg(receiving_yards) OVER last6 AS avg_rec_yds_l6,
    avg(targets)         OVER last6 AS avg_targets_l6,
    avg(receptions)      OVER last6 AS avg_receptions_l6,
    -- actuals for this game (the thing we are trying to predict)
    passing_yards,
    rushing_yards,
    receiving_yards,
    receptions
FROM stg_player_week
WINDOW last6 AS (
    PARTITION BY player_id
    ORDER BY season, week
    ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING   -- ends BEFORE the current game: no leakage
)
ORDER BY player_id, season, week;


-- -----------------------------------------------------------------------------
-- player_projections
-- Grain: player_id, season, week, market
--
-- Markets and who is eligible:
--   pass_yds   (QB)          <- passing_yards
--   rush_yds   (QB, RB)      <- rushing_yards
--   rec_yds    (RB, WR, TE)  <- receiving_yards
--   receptions (RB, WR, TE)  <- receptions
--
-- projection = the player's last-6-game average of that same stat. Deliberately
-- simple. It is NULL until the player has at least 3 prior games.
--
-- model_sd: how wrong have projections been, per market? It is the sample
-- standard deviation of (actual - projection), POOLED ACROSS PLAYERS, using
-- only STRICTLY EARLIER weeks (RANGE ... 1 PRECEDING on week_index excludes
-- every row from the current week, not just the current row). It is NULL until
-- at least 5 earlier residuals exist.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE player_projections AS
WITH
-- Wide -> long: one row per eligible market.
-- (DuckDB's UNPIVOT could do this, but four plain SELECTs glued with UNION ALL
-- make the per-market position filters obvious.)
by_market AS (
    SELECT player_id, season, week, week_index, position, team, n_prior_games,
           'pass_yds' AS market,
           CASE WHEN n_prior_games >= 3 THEN avg_pass_yds_l6 END AS projection,
           passing_yards AS actual
    FROM player_features
    WHERE position = 'QB'

    UNION ALL

    SELECT player_id, season, week, week_index, position, team, n_prior_games,
           'rush_yds' AS market,
           CASE WHEN n_prior_games >= 3 THEN avg_rush_yds_l6 END AS projection,
           rushing_yards AS actual
    FROM player_features
    WHERE position IN ('QB', 'RB')

    UNION ALL

    SELECT player_id, season, week, week_index, position, team, n_prior_games,
           'rec_yds' AS market,
           CASE WHEN n_prior_games >= 3 THEN avg_rec_yds_l6 END AS projection,
           receiving_yards AS actual
    FROM player_features
    WHERE position IN ('RB', 'WR', 'TE')

    UNION ALL

    SELECT player_id, season, week, week_index, position, team, n_prior_games,
           'receptions' AS market,
           CASE WHEN n_prior_games >= 3 THEN avg_receptions_l6 END AS projection,
           CAST(receptions AS DOUBLE) AS actual
    FROM player_features
    WHERE position IN ('RB', 'WR', 'TE')
),

-- Residual = how far off the projection was. NULL when there is no projection,
-- and stddev_samp / count ignore NULLs.
with_resid AS (
    SELECT
        *,
        actual - projection AS residual
    FROM by_market
)

SELECT
    player_id,
    season,
    week,
    week_index,
    position,
    team,
    market,
    n_prior_games,
    projection,
    actual,
    residual,
    stddev_samp(residual) OVER prior_weeks AS sd_prior,
    count(residual)       OVER prior_weeks AS n_sd_obs,
    CASE
        WHEN count(residual) OVER prior_weeks >= 5
        THEN stddev_samp(residual) OVER prior_weeks
    END AS model_sd
FROM with_resid
WINDOW prior_weeks AS (
    PARTITION BY market
    ORDER BY week_index
    RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING  -- strictly earlier weeks only
)
ORDER BY market, player_id, season, week;
