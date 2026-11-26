-- =============================================================================
-- 04_eval.sql
--
-- Builds:
--   bet_results          one row per MATCHED market_lines row (odds_id)
--                        which side the model picks, CLV, and the graded result
--   clv_summary          one row per market
--   calibration_buckets  one row per 0.1-wide bucket of p_over_model that has bets
--
-- Input:  market_lines (03_market.sql); macro american_profit (03_market.sql)
--
-- REMINDER: while the odds come from the FAKE odds fixture, every
-- number in these tables is meaningless. The SQL is real; the odds are not.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- bet_results
-- Grain: odds_id (only lines with match_status = 'matched')
--
-- One hypothetical 1-unit bet per line, placed at the OPEN price.
--
-- CLV (closing line value) = did the market move toward our side after we bet?
--   clv_line_pts: points the line moved in our favour
--                 over  -> close_line - open_line   (line went up = good)
--                 under -> open_line - close_line   (line went down = good)
--   clv_prob:     change in our side's no-vig probability, open -> close.
--                 ONLY when the line did not move: no-vig probabilities at
--                 different lines are not comparable, so otherwise NULL.
--   beat_close:   line moved our way, or same line and our side got pricier.
--                 NULL when the close is unknown (close_line or a close price
--                 missing/invalid): we can't say whether we beat it.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE bet_results AS
WITH
-- Pick a side: bet the over when the model's P(over) is at least the market's
-- fair P(over) at the open, otherwise the under.
decide AS (
    SELECT
        odds_id,
        player_id,
        season,
        week,
        market,
        open_line,
        close_line,
        open_over_price,
        open_under_price,
        projection,
        model_sd,
        actual,
        p_over_model,
        nv_open_over,
        nv_open_under,
        nv_close_over,
        nv_close_under,
        p_over_model - nv_open_over AS edge_over,
        CASE WHEN p_over_model - nv_open_over >= 0 THEN 'over' ELSE 'under' END AS side
    FROM market_lines
    WHERE match_status = 'matched'
),

-- Translate everything to "our side" so the rest needs no over/under branches.
side_probs AS (
    SELECT
        *,
        CASE WHEN side = 'over' THEN p_over_model    ELSE 1 - p_over_model END AS side_model_prob,
        CASE WHEN side = 'over' THEN nv_open_over     ELSE nv_open_under     END AS side_open_prob,
        CASE WHEN side = 'over' THEN nv_close_over    ELSE nv_close_under    END AS side_close_prob,
        CASE WHEN side = 'over' THEN open_over_price  ELSE open_under_price  END AS side_open_price,
        CASE WHEN side = 'over' THEN close_line - open_line
                                ELSE open_line - close_line                  END AS clv_line_pts
    FROM decide
),

-- Grade the bet at the open line. A whole-number line can land exactly -> push.
-- 'push' ONLY on an exact tie; if actual or open_line is NULL every comparison
-- is NULL and result stays NULL (a pipeline check asserts that never happens).
graded AS (
    SELECT
        *,
        CASE WHEN open_line = close_line
             THEN side_close_prob - side_open_prob
        END AS clv_prob,
        CASE
            WHEN actual > open_line THEN 'over'
            WHEN actual < open_line THEN 'under'
            WHEN actual = open_line THEN 'push'
        END AS result
    FROM side_probs
)

SELECT
    odds_id,
    player_id,
    season,
    week,
    market,
    open_line,
    close_line,
    projection,
    model_sd,
    actual,
    p_over_model,
    nv_open_over,
    edge_over,
    side,
    side_model_prob,
    side_open_prob,
    side_close_prob,
    side_open_price,
    clv_line_pts,
    clv_prob,
    CASE
        -- close unknown -> can't tell (nv_close_* is NULL if a close price is
        -- missing or invalid)
        WHEN close_line IS NULL OR nv_close_over IS NULL THEN NULL
        -- coalesce: clv_prob is NULL when the line moved; then only the line counts
        ELSE clv_line_pts > 0 OR (clv_line_pts = 0 AND coalesce(clv_prob > 0, FALSE))
    END AS beat_close,
    result,
    -- NULL on a push: it was neither won nor lost
    CASE WHEN result = 'push' THEN NULL ELSE result = side END AS side_won,
    -- profit on a 1-unit stake at the side's OPEN price; push returns the stake
    CASE
        WHEN result IS NULL  THEN NULL
        WHEN result = 'push' THEN 0.0
        WHEN result = side   THEN american_profit(side_open_price)
        ELSE -1.0
    END AS profit_units
FROM graded
ORDER BY odds_id;


-- -----------------------------------------------------------------------------
-- clv_summary
-- Grain: market
--
-- Two ROI conventions, both 1 unit staked per bet:
--   roi           = total profit / non-push bets  (a push hands the stake back,
--                   so only decided bets count as money risked)
--   roi_all_bets  = total profit / all bets       (every bet counts as a stake)
-- They only differ when there are pushes.
--
-- pct_beat_close: avg of beat_close as 1.0/0.0. avg() skips NULLs, so bets
-- whose close is unknown are left out instead of counted as "didn't beat".
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE clv_summary AS
SELECT
    market,
    count(*)                                              AS n_bets,
    count(*) FILTER (WHERE result = 'push')               AS n_pushes,
    avg(clv_line_pts)                                     AS avg_clv_line_pts,
    avg(clv_prob)                                         AS avg_clv_prob,
    avg(beat_close::DOUBLE)                               AS pct_beat_close,
    sum(profit_units)                                     AS total_profit_units,
    sum(profit_units)
        / nullif(count(*) FILTER (WHERE result <> 'push'), 0) AS roi,
    sum(profit_units) / count(*)                          AS roi_all_bets
FROM bet_results
GROUP BY market
ORDER BY market;


-- -----------------------------------------------------------------------------
-- calibration_buckets
-- Grain: bucket_lo (only buckets that contain bets)
--
-- Is the model honest? Within each bucket of predicted P(over), compare the
-- average prediction to how often the over actually hit. gap > 0 means the
-- model was too pessimistic about overs in that bucket.
-- Pushes are excluded: they are neither an over nor an under.
-- least(..., 0.9) puts p = 1.0 in the top bucket instead of an 11th one.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE calibration_buckets AS
WITH
-- Assign each decided bet to a 0.1-wide bucket.
bucketed AS (
    SELECT
        least(floor(p_over_model * 10) / 10, 0.9)       AS bucket_lo,
        p_over_model,
        nv_open_over,
        CASE WHEN result = 'over' THEN 1.0 ELSE 0.0 END AS went_over
    FROM bet_results
    WHERE result <> 'push'
),

-- Average within each bucket.
per_bucket AS (
    SELECT
        bucket_lo,
        count(*)          AS n_bets,
        avg(p_over_model) AS avg_model_prob,
        avg(went_over)    AS actual_over_rate,
        avg(nv_open_over) AS avg_market_prob
    FROM bucketed
    GROUP BY bucket_lo
)

SELECT
    bucket_lo,
    round(bucket_lo + 0.1, 1)           AS bucket_hi,
    n_bets,
    avg_model_prob,
    actual_over_rate,
    avg_market_prob,
    actual_over_rate - avg_model_prob   AS gap
FROM per_bucket
ORDER BY bucket_lo;
