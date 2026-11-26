# NFL-Prop-project-

Backtest player-prop lines (passing / rushing / receiving yards, receptions)
against a simple rolling-average model, using DuckDB SQL over nflverse weekly
player stats.

> **Warning:** the only odds in this repo are `tests/fixtures/FAKE_odds.csv`,
> which are invented. Every CLV / ROI / calibration number the pipeline prints
> right now is **meaningless**. Real historical lines are a future data source.

## Quickstart

```bash
pip install -r requirements.txt
make data       # download nflverse player_stats 2021-2024 into data/raw/
make pipeline   # build data/nfl.duckdb and print a summary
make test       # offline tests (no network, no data/ needed)
```

`make data SEASONS="2023 2024"` downloads only some seasons.
Open the result with `duckdb data/nfl.duckdb` (all outputs are plain tables).

## Layout

| path | what |
|---|---|
| `scripts/get_data.py` | downloads `player_stats_{season}.parquet` from the nflverse release |
| `src/resolve.py` | name/team normalization and join keys |
| `src/pipeline.py` | registers UDFs, creates the raw views, runs `sql/NN_*.sql` in order, runs checks |
| `sql/01_staging.sql` | `stg_player_week`, `player_name_xwalk` |
| `sql/02_features.sql` | `player_features`, `player_projections` |
| `sql/03_market.sql` | `stg_odds`, `market_lines` |
| `sql/04_eval.sql` | `bet_results`, `clv_summary`, `calibration_buckets` |

## Table lineage

```
raw_player_stats (view: parquet)          raw_odds (view: csv)
   |                                          |
   +--> stg_player_week --> player_features   |
   |         |                  |             |
   |         v                  v             v
   |    player_name_xwalk   player_projections  stg_odds
   |         \                  |              /
   |          +-----------> market_lines <----+
   |                             |
   |                             v
   |                        bet_results
   |                        /         \
   |                 clv_summary   calibration_buckets
```

Linear form: `raw_player_stats -> stg_player_week / player_name_xwalk ->
player_features -> player_projections -> stg_odds -> market_lines ->
bet_results -> clv_summary / calibration_buckets`.

## Model in one paragraph

A player's projection for a stat is the average of that stat over his last 6
regular-season games (crossing season boundaries, never including the game
itself), once he has at least 3. The spread is the standard deviation of past
projection errors for that market across all players, using only earlier
weeks. P(over) at the opening line comes from a normal approximation; the bet
side is whichever side the model likes more than the no-vig market price.
