# Test fixtures

## `FAKE_odds.csv` — invented odds, NOT real data

Every line, price and book in this file was made up to exercise edge cases
in the SQL. Any CLV, ROI or calibration number computed from it is
**meaningless**. Every row has `source = FAKE`, and `src/pipeline.py` prints a
loud banner whenever it runs on a file with `FAKE` in its name.

Schema (one row per player-market line):

| column | type | meaning |
|---|---|---|
| source | VARCHAR | where the line came from (`FAKE` here) |
| odds_id | INTEGER | unique row id |
| book | VARCHAR | sportsbook |
| season, week | INTEGER | NFL season and regular-season week |
| player_name | VARCHAR | name as the book spells it |
| team | VARCHAR | team as the book spells it (any alias / full name) |
| market | VARCHAR | `pass_yds`, `rush_yds`, `rec_yds`, `receptions` |
| open_line, close_line | DOUBLE | the line at open and at close |
| open_over_price, open_under_price, close_over_price, close_under_price | INTEGER | American odds |

What each row tests:

| odds_id | purpose | expected match_status |
|---|---|---|
| 1 | "D.J. Moore" punctuation; line moves 70.5 -> 72.5 | matched |
| 2 | same player; same line, price moves | matched |
| 3 | override "Hollywood Brown" -> Marquise Brown; ARZ -> ARI; week 2 needs prior-season history | matched |
| 4 | traded player, now on KC | matched |
| 5 | whole-number line 5.0, actual 5 -> push; team given as "Kansas City Chiefs" | matched |
| 6 | override "Ken Walker" -> Kenneth Walker III | matched |
| 7 | same line, price move -> probability CLV | matched |
| 8 | JAC -> JAX | matched |
| 9 | pass_yds market (7 earlier residuals >= 5 needed for model_sd) | matched |
| 10 | two "Mike Williams" on LAC in 2023 | ambiguous_name |
| 11 | unknown player | no_player |
| 12 | Kelce has no game in week 7 | no_projection |

## Stats fixture

There is no stats file on disk. `tests/conftest.py::make_stats_frame()` builds a
small deterministic table with the real nflverse `player_stats` columns and the
tests write it to a temporary parquet file. See its docstring for the formulas
and the special cases (bye week, NaN stat, playoff game, kicker, trade, name
collision).
