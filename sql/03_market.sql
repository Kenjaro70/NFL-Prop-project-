-- =============================================================================
-- 03_market.sql
--
-- Builds:
--   stg_odds      one row per odds CSV row (odds_id)
--                 typed odds + resolution key + no-vig probabilities
--   market_lines  exactly one row per stg_odds row (odds_id)
--                 each line joined to its player and projection, with a
--                 match_status and the model's P(over)
--
-- Inputs: raw_odds            (view over the odds CSV, made by src/pipeline.py)
--         player_name_xwalk   (01_staging.sql)
--         player_projections  (02_features.sql)
-- UDFs:   normalize_team, build_key, normal_cdf  (Python, src/pipeline.py)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Macros (SQL-only helper functions).
--
-- american_to_prob: implied probability of an American price, vig included.
--   -110 -> 110/210 = 0.5238    +150 -> 100/250 = 0.40    +100 -> 0.50
-- american_profit: profit on a winning 1-unit stake.
--   -110 -> 100/110 = 0.909     +150 -> 1.50
-- -----------------------------------------------------------------------------
CREATE OR REPLACE MACRO american_to_prob(price) AS
    CASE
        WHEN price < 0 THEN -price / (-price + 100.0)
        ELSE 100.0 / (price + 100.0)
    END;

CREATE OR REPLACE MACRO american_profit(price) AS
    CASE
        WHEN price > 0 THEN price / 100.0
        ELSE 100.0 / -price
    END;


-- -----------------------------------------------------------------------------
-- stg_odds
-- Grain: odds_id (one row per CSV row)
--
-- No-vig ("fair") probability: the book's two implied probabilities add up to
-- more than 1 (that extra is the vig). Dividing each by their sum removes it:
--   nv_over = imp_over / (imp_over + imp_under)
-- Done for both the open and the close.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE stg_odds AS
WITH
-- Keep the raw columns, but rename the book's team spelling to team_raw and
-- add the canonical team + the join key (same build_key as the stats side).
keyed AS (
    SELECT
        raw_odds.* RENAME (team AS team_raw),
        normalize_team(raw_odds.team)               AS team,
        build_key(raw_odds.player_name, raw_odds.team) AS join_key
    FROM raw_odds
),

-- Implied probabilities straight from the prices (still include the vig).
implied AS (
    SELECT
        *,
        american_to_prob(open_over_price)   AS imp_open_over,
        american_to_prob(open_under_price)  AS imp_open_under,
        american_to_prob(close_over_price)  AS imp_close_over,
        american_to_prob(close_under_price) AS imp_close_under
    FROM keyed
),

-- Remove the vig by normalising each pair to sum to 1.
no_vig AS (
    SELECT
        *,
        imp_open_over   / (imp_open_over  + imp_open_under)  AS nv_open_over,
        imp_open_under  / (imp_open_over  + imp_open_under)  AS nv_open_under,
        imp_close_over  / (imp_close_over + imp_close_under) AS nv_close_over,
        imp_close_under / (imp_close_over + imp_close_under) AS nv_close_under
    FROM implied
)

SELECT
    odds_id,
    source,
    book,
    season,
    week,
    player_name,
    team_raw,
    team,
    join_key,
    market,
    open_line,
    open_over_price,
    open_under_price,
    close_line,
    close_over_price,
    close_under_price,
    imp_open_over,
    imp_open_under,
    imp_close_over,
    imp_close_under,
    nv_open_over,
    nv_open_under,
    nv_close_over,
    nv_close_under
FROM no_vig
ORDER BY odds_id;


-- -----------------------------------------------------------------------------
-- market_lines
-- Grain: odds_id - EXACTLY one row per stg_odds row.
--
-- Only LEFT JOINs, and every right-hand side is unique on its join columns,
-- so a line can never be duplicated ("fan out") or dropped. Lines that cannot
-- be used are kept and labelled by match_status:
--   'no_player'       no player has this (season, join_key)
--   'ambiguous_name'  more than one player has it -> never guess
--   'no_projection'   player found, but no projection / model_sd that week
--   'matched'         usable
--
-- p_over_model is evaluated at the OPEN line, because the bet is assumed to be
-- placed at the open:
--   P(actual > line) = 1 - Phi((open_line - projection) / model_sd)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE market_lines AS
WITH
-- Collapse the crosswalk to one row per (season, join_key). It is unique on
-- those columns, so joining it to the odds cannot fan out. A traded player has
-- two keys (one per team) and each maps back to him.
xwalk_by_key AS (
    SELECT
        season,
        join_key,
        count(DISTINCT player_id) AS n_players,
        min(player_id)            AS any_player_id
    FROM player_name_xwalk
    GROUP BY season, join_key
),

-- Attach a player_id, but only when exactly one player owns the key.
odds_player AS (
    SELECT
        o.*,
        x.n_players,
        CASE WHEN x.n_players = 1 THEN x.any_player_id END AS player_id
    FROM stg_odds AS o
    LEFT JOIN xwalk_by_key AS x
           ON x.season = o.season
          AND x.join_key = o.join_key
),

-- Attach the projection for that player, week and market.
-- player_projections is unique on (player_id, season, week, market).
odds_proj AS (
    SELECT
        op.*,
        pp.projection,
        pp.model_sd,
        pp.n_prior_games,
        pp.actual
    FROM odds_player AS op
    LEFT JOIN player_projections AS pp
           ON pp.player_id = op.player_id
          AND pp.season    = op.season
          AND pp.week      = op.week
          AND pp.market    = op.market
),

-- Label each line and compute the model probability.
labelled AS (
    SELECT
        *,
        CASE
            WHEN n_players IS NULL THEN 'no_player'
            WHEN n_players > 1     THEN 'ambiguous_name'
            -- model_sd = 0 would divide by zero below; treat it as unusable
            WHEN projection IS NULL OR model_sd IS NULL OR model_sd = 0
                                   THEN 'no_projection'
            ELSE 'matched'
        END AS match_status,
        1 - normal_cdf((open_line - projection) / nullif(model_sd, 0)) AS p_over_model
    FROM odds_proj
)

SELECT
    odds_id,
    match_status,
    source,
    book,
    season,
    week,
    player_name,
    team_raw,
    team,
    join_key,
    player_id,
    market,
    open_line,
    open_over_price,
    open_under_price,
    close_line,
    close_over_price,
    close_under_price,
    nv_open_over,
    nv_open_under,
    nv_close_over,
    nv_close_under,
    projection,
    model_sd,
    n_prior_games,
    actual,
    p_over_model
FROM labelled
ORDER BY odds_id;
