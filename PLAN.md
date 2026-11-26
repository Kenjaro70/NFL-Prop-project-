# PLAN.md — NFL prop pipeline

Produced by the planning agent; the writer follows it. Sections: facts verified,
decisions, file specs, work order, risks.

## 0. Verified facts
- Real nflverse `player_stats_{season}.parquet` columns (identical 2021–2024): `player_id, player_name, player_display_name, position, position_group, headshot_url, recent_team, season (INT), week (INT), season_type ('REG'/'POST'), opponent_team, completions (INT), attempts (INT), passing_yards (DOUBLE), …, carries (INT), rushing_yards (DOUBLE), …, receptions (INT), targets (INT), receiving_yards (DOUBLE), …`.
- ~5.6k rows/season; REG weeks 1–18, POST up to 22. Mostly QB/RB/WR/TE plus a few FB/P/defense.
- No duplicate `(player_id, season, week)`; `recent_team` already nflverse codes; stat columns have no NULLs (keep `coalesce` anyway). 0 build_key collisions within a season in 2021/2023/2024.
- **DuckDB 2.0.1 gotcha:** `con.create_function(...)` with default null handling errors when the UDF returns None → register every UDF with `null_handling="special"`.
- DuckDB has **no erf / normal CDF** → register Python UDF `normal_cdf` via `math.erf`.
- `con.execute(text)` runs a whole multi-statement `.sql` file with comments.
- `ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING` → NULL on first row, correct partial averages after.
- scipy not installed — don't add it. Python `urllib` works through the proxy.

## 1. Decisions (each gets a comment in code)
| Topic | Decision | Why |
|---|---|---|
| Rolling partition | `PARTITION BY player_id ORDER BY season, week` — **crosses seasons** | Within-season would leave weeks 1–3 of every season (~17% of sample, softest lines) without projections. Cost: offseason role changes. |
| Window | last **6 games**: `ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING`; projection requires `n_prior_games >= 3` else NULL | Never sees itself. Counts games, not weeks (byes/DNPs skipped). |
| Season type | REG only in staging (playoffs never enter the window) | Simple; matches spec. |
| Projection | projection = rolling avg of the same stat. Markets/eligibility: `pass_yds`(QB)←passing_yards; `rush_yds`(QB,RB)←rushing_yards; `rec_yds`(RB,WR,TE)←receiving_yards; `receptions`(RB,WR,TE)←receptions. avg_targets/avg_carries kept as features. | Deliberately simple. |
| Model SD | residual = actual − projection; per market `stddev_samp(residual) OVER (PARTITION BY market ORDER BY week_index RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)`, `week_index = season*100 + week`; also count; usable only if `n_sd_obs >= 5` | Only strictly earlier weeks → no leakage. |
| Model prob | `p_over_model = 1 - normal_cdf((open_line - projection) / model_sd)` at the **open** line (bet placed at open) | Normal approx. |
| No-vig | `american_to_prob(p)` = `-p/(-p+100)` if p<0 else `100/(p+100)`; nv over = `imp_over/(imp_over+imp_under)`, for open and close | |
| Side | `edge_over = p_over_model - nv_open_over`; side = 'over' if `edge_over >= 0` else 'under' | |
| CLV | `clv_line_pts = close_line - open_line` (over) / `open_line - close_line` (under). `clv_prob = side_close_nv - side_open_nv` **only when open_line = close_line**, else NULL. `beat_close = clv_line_pts > 0 OR (clv_line_pts = 0 AND clv_prob > 0)` | No-vig probs at different lines aren't comparable. |
| Outcome | graded at open_line: actual > line → 'over', < → 'under', = → 'push'; `side_won` NULL on push; `profit_units` at side's open price: win +price/100 (price>0) or +100/−price (price<0), loss −1, push 0 | |
| Calibration | `p_over_model` vs actual over, pushes excluded; `bucket_lo = least(floor(p*10)/10, 0.9)` → 10 buckets of 0.1 | |
| Crosswalk grain | one row per `(player_id, season, join_key)`; `is_ambiguous` = >1 distinct player_id share `(season, join_key)`. Ambiguous keys are **never matched**. | |
| Outputs | every SQL output is a TABLE (`CREATE OR REPLACE TABLE`), never a view — a saved .duckdb reopens without UDFs. Only `raw_player_stats` and `raw_odds` are views (created by pipeline.py). | |
| UDFs | `normalize_name, normalize_team, canonical_name, build_key(name, team), normal_cdf`, all `null_handling="special"` | Extends the original contract: `join_key` must equal `resolve.build_key`; DuckDB lacks erf. |

## 2. Files

### 2.1 `scripts/get_data.py`
- stdlib + duckdb (verification) only; no `src` import.
- `BASE_URL = "https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats_{season}.parquet"`, `DEFAULT_SEASONS = [2021, 2022, 2023, 2024]`, `RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"`.
- `url_for_season(season) -> str`; `dest_for_season(season, out_dir) -> Path` (`out_dir / f"player_stats_{season}.parquet"`).
- `download(url, dest, force=False) -> bool`: skip (False) if exists and not force; else `urllib.request.urlopen(url, timeout=60)` → `dest.with_suffix(".parquet.part")` → `Path.replace(dest)`; True.
- `verify(dest) -> int`: count rows with duckdb `read_parquet`; raise if 0.
- `main(argv=None)`: argparse `--seasons` (nargs="+", int, default), `--out` (default RAW_DIR), `--force`; mkdir; print one line per season (rows, downloaded/cached).
- Docstring: notes the newer `stats_player/stats_player_week_{season}.parquet` (different columns) is NOT used.

### 2.2 `src/pipeline.py`
- `ROOT`, `SQL_DIR = ROOT/"sql"`, `DEFAULT_STATS = ROOT/"data/raw/player_stats_*.parquet"`, `DEFAULT_ODDS = ROOT/"tests/fixtures/FAKE_odds.csv"`, `DEFAULT_DB = ROOT/"data/nfl.duckdb"`.
- `normal_cdf(x)` → `0.5*(1+math.erf(x/math.sqrt(2)))`, None→None.
- `register_udfs(con)` — **always** `null_handling="special"`: `normalize_name(VARCHAR)->VARCHAR`, `normalize_team(VARCHAR)->VARCHAR`, `canonical_name(VARCHAR)->VARCHAR`, `build_key(VARCHAR,VARCHAR)->VARCHAR`, `normal_cdf(DOUBLE)->DOUBLE`.
- `_sql_str(path)`: resolved path with `'` doubled.
- `create_raw_views(con, stats_path, odds_path)`: if `glob.glob(str(stats_path))` empty → `FileNotFoundError("No stats parquet at ... run `make data`")`; `CREATE OR REPLACE VIEW raw_player_stats AS SELECT * FROM read_parquet('<abs>', union_by_name = true)`; `CREATE OR REPLACE VIEW raw_odds AS SELECT * FROM read_csv('<abs>', header = true, columns = {...})` with types `source VARCHAR, odds_id INTEGER, book VARCHAR, season INTEGER, week INTEGER, player_name VARCHAR, team VARCHAR, market VARCHAR, open_line DOUBLE, open_over_price INTEGER, open_under_price INTEGER, close_line DOUBLE, close_over_price INTEGER, close_under_price INTEGER`.
- `sql_files(sql_dir)` → `sorted(sql_dir.glob("[0-9][0-9]_*.sql"))`.
- `run_sql_files(con, sql_dir)`: `con.execute(path.read_text())` each; re-raise with filename; return names.
- `CHECKS` (name, query that must return 0): `stg_player_week_dupes`, `features_rowcount_mismatch` (player_features vs stg), `projection_dupes` on `(player_id, season, week, market)`, `market_fanout` (market_lines − stg_odds), `bet_rowcount_mismatch` (bet_results − matched market_lines).
- `run_checks(con)`: AssertionError listing all failures.
- `run_pipeline(stats_path=DEFAULT_STATS, odds_path=DEFAULT_ODDS, db_path=":memory:")`: connect → UDFs → views → SQL → checks → return con.
- `summarize(con)`: row counts of `stg_player_week, player_name_xwalk, player_features, player_projections, stg_odds, market_lines, bet_results, clv_summary, calibration_buckets`; `match_status` counts; `report_unmatched(stg_odds season||':'||join_key, xwalk season||':'||join_key, label="odds")`.
- `main(argv=None)`: `--stats`, `--odds`, `--db` (default DEFAULT_DB); loud banner `*** USING FAKE ODDS FIXTURE - CLV/ROI NUMBERS ARE MEANINGLESS ***` if odds filename contains FAKE; mkdir db parent; run; summarize. `python -m src.pipeline`.

### 2.3 `sql/01_staging.sql`
Header: what it builds, input `raw_player_stats`, UDFs used.
- **`stg_player_week`** (grain player_id, season, week). CTEs `reg_season_skill` (`season_type = 'REG' AND position IN ('QB','RB','WR','TE')`) → `cleaned`. Columns: `player_id, player_display_name, position, team = normalize_team(recent_team), opponent = normalize_team(opponent_team), season, week, week_index = season*100 + week`, counts `attempts, completions, carries, targets, receptions` as `CAST(coalesce(x,0) AS INTEGER)`, yards `passing_yards, rushing_yards, receiving_yards` as `CAST(coalesce(x,0) AS DOUBLE)`, `name_key = canonical_name(player_display_name)`, `join_key = build_key(player_display_name, recent_team)`. Comment: no dedupe here — duplicates fail loudly in checks/tests.
- **`player_name_xwalk`** (grain player_id, season, join_key). CTE `player_team_seasons`: `player_id, season, team, any_value(player_display_name), name_key, join_key, min(week) first_week, max(week) last_week, count(*) n_games` grouped by player_id, season, team, name_key, join_key. Add `is_ambiguous` = count distinct player_id per (season, join_key) > 1 (via separate GROUP BY CTE joined back if DISTINCT-in-window unsupported). Comment: traded player → one row per team.

### 2.4 `sql/02_features.sql`
- **`player_features`** (one row per stg row), named window:
  ```sql
  WINDOW last6 AS (
      PARTITION BY player_id
      ORDER BY season, week
      ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING   -- ends BEFORE the current game: no leakage
  )
  ```
  Columns: `player_id, season, week, week_index, position, team, game_seq = row_number() OVER (PARTITION BY player_id ORDER BY season, week), n_prior_games = count(*) OVER last6, avg_pass_yds_l6, avg_rush_yds_l6, avg_carries_l6, avg_rec_yds_l6, avg_targets_l6, avg_receptions_l6`, plus actuals `passing_yards, rushing_yards, receiving_yards, receptions`. Comment block: games not weeks; crosses seasons; why 1 PRECEDING.
- **`player_projections`** (long; grain player_id, season, week, market). CTE `by_market` = UNION ALL of 4 SELECTs (with position filters): `market, projection = CASE WHEN n_prior_games >= 3 THEN avg_x_l6 END, actual, n_prior_games, week_index` (comment: UNPIVOT could do this; UNION ALL is clearer). CTE `with_resid`: `residual = actual - projection`. Final adds:
  ```sql
  stddev_samp(residual) OVER prior_weeks AS sd_prior,
  count(residual)       OVER prior_weeks AS n_sd_obs,
  CASE WHEN count(residual) OVER prior_weeks >= 5 THEN stddev_samp(residual) OVER prior_weeks END AS model_sd
  WINDOW prior_weeks AS (PARTITION BY market ORDER BY week_index
                         RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
  ```
  Comment: SD pooled across players, strictly earlier weeks.

### 2.5 `sql/03_market.sql`
- Macros: `american_to_prob(price)`, `american_profit(price)` (profit per 1 unit staked).
- **`stg_odds`** (one row per CSV row): `raw_odds.*` with `team → team_raw`, `team = normalize_team(team_raw)`, `join_key = build_key(player_name, team_raw)`; CTEs `implied` → `no_vig` → `nv_open_over, nv_open_under, nv_close_over, nv_close_under`.
- **`market_lines`** (exactly one row per stg_odds row; LEFT JOINs only): CTE `xwalk_by_key` (`season, join_key, count(DISTINCT player_id) n_players, min(player_id) any_player_id` grouped — unique per key so can't fan out); `odds_player` (stg_odds LEFT JOIN xwalk_by_key USING (season, join_key); `player_id = CASE WHEN n_players = 1 THEN any_player_id END`); `odds_proj` (LEFT JOIN player_projections on player_id, season, week, market). `match_status`: `'no_player'` (n_players NULL), `'ambiguous_name'` (>1), `'no_projection'` (projection or model_sd NULL), else `'matched'`. `p_over_model = 1 - normal_cdf((open_line - projection) / model_sd)`. Keep odds/lines/prices, nv_* probs, projection, model_sd, n_prior_games, actual, p_over_model, player_id, join_key, team_raw, team, source, book.

### 2.6 `sql/04_eval.sql`
- **`bet_results`** from matched market_lines; CTEs `decide` → `side_probs` → `graded`. Columns: `odds_id, player_id, season, week, market, open_line, close_line, projection, model_sd, actual, p_over_model, nv_open_over, edge_over, side, side_model_prob, side_open_prob, side_close_prob, side_open_price, clv_line_pts, clv_prob, beat_close, result, side_won, profit_units`.
- **`clv_summary`** by market: `n_bets, n_pushes, avg_clv_line_pts, avg_clv_prob, pct_beat_close, total_profit_units, roi`.
- **`calibration_buckets`** (non-push): `bucket_lo, bucket_hi, n_bets, avg_model_prob, actual_over_rate, avg_market_prob, gap = actual_over_rate - avg_model_prob`, ordered.

### 2.7 `tests/fixtures/FAKE_odds.csv` (all `source = FAKE`)
```
source,odds_id,book,season,week,player_name,team,market,open_line,open_over_price,open_under_price,close_line,close_over_price,close_under_price
FAKE,1,fakebook,2023,4,D.J. Moore,CHI,rec_yds,70.5,-110,-110,72.5,-115,-105
FAKE,2,fakebook,2023,5,D.J. Moore,CHI,receptions,5.5,100,-120,5.5,-120,100
FAKE,3,fakebook,2023,2,Hollywood Brown,ARZ,rec_yds,58.5,-110,-110,56.5,-110,-110
FAKE,4,fakebook,2023,5,Hollywood Brown,KC,receptions,4.5,-130,110,5.5,105,-125
FAKE,5,fakebook,2023,5,Travis Kelce,Kansas City Chiefs,receptions,5.0,-110,-110,5.0,-105,-115
FAKE,6,fakebook,2023,3,Ken Walker,SEA,rush_yds,72.5,-110,-110,74.5,-110,-110
FAKE,7,fakebook,2023,4,Christian McCaffrey,SF,rush_yds,85.5,-115,-105,85.5,-125,105
FAKE,8,fakebook,2023,6,Evan Engram,JAC,rec_yds,44.5,-110,-110,43.5,-110,-110
FAKE,9,fakebook,2023,6,Patrick Mahomes,KC,pass_yds,268.5,-110,-110,270.5,-110,-110
FAKE,10,fakebook,2023,3,Mike Williams,LAC,rec_yds,52.5,-110,-110,52.5,-110,-110
FAKE,11,fakebook,2023,2,Joe Nobody,NYJ,rec_yds,30.5,-110,-110,30.5,-110,-110
FAKE,12,fakebook,2023,7,Travis Kelce,KC,receptions,5.5,-110,-110,5.5,-110,-110
```
Rows: 1–2 "D.J." punctuation (line move / same line + price move); 3 override + ARZ→ARI, week-2 needs prior-season history; 4 traded to KC; 5 whole-number push (actual 5) + full team name; 6 override Ken Walker; 7 same line, price move; 8 JAC→JAX; 9 pass_yds (7 prior residuals ≥ 5); 10 ambiguous; 11 unknown player; 12 no game that week → no_projection.
**Expected match_status: matched 9, ambiguous_name 1, no_player 1, no_projection 1.**

### 2.8 `tests/fixtures/README.md`
FAKE_odds.csv is invented; numbers from it are meaningless; schema + per-row purpose; stats fixture is generated in `tests/conftest.py`.

### 2.9 `tests/conftest.py`
`make_stats_frame() -> pd.DataFrame`, deterministic. Columns: `player_id, player_name, player_display_name, position, position_group, recent_team, season, week, season_type, opponent_team ('NYJ'), completions, attempts, passing_yards, carries, rushing_yards, receptions, targets, receiving_yards`. Seasons 2022 & 2023, weeks 1–6, REG. Stat = yards: `base + 5*((week+season)%4)`; counts: `base + (week+season)%3`; missing stats 0. Yards float, counts int.

| id | display | pos | team | base |
|---|---|---|---|---|
| 00-0000001 | Patrick Mahomes | QB | KC | attempts 34, completions 22, passing_yards 260, carries 4, rushing_yards 18 |
| 00-0000002 | Christian McCaffrey | RB | SF | carries 18, rushing_yards 80, targets 6, receptions 5, receiving_yards 35 |
| 00-0000003 | DJ Moore | WR | CHI | targets 9, receptions 6, receiving_yards 75 |
| 00-0000004 | Marquise Brown | WR | ARI (2022 + 2023 wk1–3), KC (2023 wk4–6) | targets 8, receptions 5, receiving_yards 55 |
| 00-0000005 | Travis Kelce | TE | KC | targets 8, receptions 5, receiving_yards 60 |
| 00-0000006 | Kenneth Walker III | RB | SEA | carries 16, rushing_yards 70, targets 2, receptions 1, receiving_yards 8 |
| 00-0000007 | Evan Engram | TE | JAX | targets 6, receptions 4, receiving_yards 40 |
| 00-0000008 | Justin Tucker | K | BAL | all 0 (filtered out) |
| 00-0000009 | Mike Williams | WR | LAC | targets 7, receptions 4, receiving_yards 50 |
| 00-0000010 | Mike Williams | WR | LAC | **2023 only**; targets 1, receptions 1, receiving_yards 6 (collision) |

Specials: drop Mahomes 2022 wk3 (bye); Kelce 2022 wk2 `receiving_yards = NaN`; add McCaffrey 2023 wk19 `season_type='POST'`.
Raw = 114 rows; **stg_player_week must = 101** (8×12 − 1 + 6).
Fixtures: `stats_frame` (fresh copy), `odds_csv`, `run_on_frame` factory `(df, tmp_path)` → parquet → `run_pipeline`, `con` (session scope via `tmp_path_factory`). `FEATURE_COLS = [n_prior_games, avg_pass_yds_l6, avg_rush_yds_l6, avg_carries_l6, avg_rec_yds_l6, avg_targets_l6, avg_receptions_l6]` — don't import from conftest; expose as fixture or redefine.

### 2.10 Tests (offline)
- `test_get_data.py`: `test_url_for_season`, `test_dest_for_season`, `test_download_skips_existing_file`.
- `test_pipeline_contract.py`: `test_sql_files_run_in_numeric_order`, `test_sql_files_contain_no_paths` (no read_parquet/read_csv/.parquet/.csv/data/), `test_udfs_registered` (`build_key('D.J. Moore','JAC')='djmoore|JAX'`, `normalize_team('zzz')` NULL), `test_normal_cdf_known_values`, `test_missing_stats_raises_helpful_error`, `test_run_checks_pass_on_fixture`.
- `test_staging.py`: `test_stg_one_row_per_player_week`, `test_stg_row_count_is_101`, `test_stg_excludes_post_and_non_skill_positions`, `test_stg_nulls_become_zero`, `test_xwalk_join_key_matches_python_build_key`, `test_xwalk_traded_player_has_row_per_team`, `test_xwalk_flags_ambiguous_names` (2023 yes, 2022 no).
- `test_features_leakage.py`: `test_features_one_row_per_player_week`, `test_rolling_avgs_match_manual_strictly_earlier_games` (pandas `shift(1).rolling(6, min_periods=1).mean()`; NaN↔NULL), `test_first_game_has_no_history`, `test_window_counts_games_not_weeks` (Mahomes 2022 wk4 n_prior 2 = mean wk1,2), `test_window_crosses_seasons` (DJ Moore 2023 wk1 = mean of 2022 wk1–6), `test_mutating_a_game_never_changes_its_own_or_earlier_features` (bump DJ Moore ≥(2023,4); rows ≤202304 unchanged incl. projection and every model_sd ≤202304; 2023 wk5 changes), `test_projections_one_row_per_player_week_market` + position eligibility.
- `test_market_eval.py`: `test_odds_fixture_is_labeled_fake`, `test_stg_odds_row_count_matches_csv` (12), `test_market_join_has_no_fanout`, `test_match_status_counts`, `test_aliases_resolve_to_expected_players` (1→…03, 3→…04, 4→…04, 5→…05, 6→…06, 8→…07), `test_american_to_prob_known_values` (−110→0.52381, +150→0.4, +100→0.5), `test_no_vig_probs_sum_to_one`, `test_bet_results_row_count_equals_matched` (9), `test_push_on_whole_number_line` (odds 5), `test_clv_sign_convention`, `test_profit_units`, `test_calibration_counts_match_non_push_bets` (8; valid bucket_lo).

### 2.11 `Makefile` (TAB-indented recipes)
```make
PY ?= python3
SEASONS ?= 2021 2022 2023 2024
.PHONY: data pipeline test
data:
	$(PY) scripts/get_data.py --seasons $(SEASONS)
pipeline:
	$(PY) -m src.pipeline --db data/nfl.duckdb
test:
	$(PY) -m pytest -q
```

### 2.12 `README.md`
Quickstart, table lineage (`raw_player_stats → stg_player_week / player_name_xwalk → player_features → player_projections → stg_odds → market_lines → bet_results → clv_summary / calibration_buckets`), FAKE-odds warning.

## 3. Work order
1. get_data.py + test. 2. pipeline.py + contract tests. 3. 01 + staging tests. 4. 02 + leakage tests. 5. FAKE csv, 03, 04 + market tests. 6. Makefile, README. 7. `make test` (resolve's 40 still pass). 8. `make data && make pipeline` on real data; report counts and unmatched keys.

## 4. Risks
1. UDF nulls need `null_handling="special"`.
2. Contract extended with `canonical_name`, `build_key`, `normal_cdf` UDFs; SQL still path-free.
3. Window counts recorded games; early-season windows reach into the prior season.
4. nflverse only has rows where a player recorded a stat → zero-stat healthy games missing (biases fringe players up); in-game injuries drag averages down.
5. Cross-season partition carries stale history across team/role changes; rookies have no projection until game 4.
6. Traded players matched on team-at-game-time; a stale book team → `no_player` (visible in report_unmatched). Unknown team alias → `|?`.
7. Name collisions flagged `is_ambiguous` and excluded, never guessed.
8. Whole-number lines push (graded 0, excluded from calibration); normal approx ignores push mass; poor fit for low-volume receptions.
9. One pooled SD per market; needs ≥5 prior residuals, so the earliest 2021 weeks get no probability.
10. Probability CLV only defined when the line didn't move; otherwise only `clv_line_pts`.
11. **All ROI/CLV/calibration numbers are from FAKE odds** — real historical lines are a future data source.
12. Pinned to the legacy `player_stats` release.
