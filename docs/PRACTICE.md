# Practice guide: learn this pipeline, then rewrite it yourself

This guide walks you through what the code does and gives you exercises that
build up to writing every SQL file from a blank page. The answers are hidden in
collapsible `<details>` blocks. Try each one before you open it.

**Contents**

0. [Setup](#0-setup)
1. [The big picture](#1-the-big-picture)
2. [Guided tour: read each file, then explain it back](#2-guided-tour)
3. [Exercises, level 1 to 4](#3-exercises)
4. [Spot the bug](#4-spot-the-bug)
5. [Rewrite drills: the tests are your grader](#5-rewrite-drills)
6. [Glossary](#6-glossary)
7. [Suggested two-week plan](#7-suggested-two-week-plan)

---

## 0. Setup

```bash
pip install -r requirements.txt
make data        # downloads 2021-2024 nflverse player stats into data/raw/
make pipeline    # builds data/nfl.duckdb (prints a loud FAKE-odds banner)
make test        # 112 tests, all offline
```

To run any query against the database:

```bash
python3 scripts/q.py "SELECT * FROM stg_player_week LIMIT 5"
python3 scripts/q.py scratch/my_query.sql     # or put the query in a file
```

The example outputs below come from the nflverse files as downloaded in
November 2026. nflverse sometimes corrects stats, so a number may move
slightly. If yours is close, you're right.

> **The odds are fake.** `tests/fixtures/FAKE_odds.csv` has 12 invented lines,
> there only so `03` and `04` have something to run on. The SQL is real; the
> CLV, ROI and calibration numbers mean nothing.

---

## 1. The big picture

```
data/raw/player_stats_*.parquet          tests/fixtures/FAKE_odds.csv
        │  (view: raw_player_stats)               │  (view: raw_odds)
        ▼                                         │
01_staging.sql                                    │
   stg_player_week   1 row / player / week        │
   player_name_xwalk 1 row / player / season / team key
        ▼                                         │
02_features.sql                                   │
   player_features    rolling last-6-game averages│
   player_projections 1 row / player / week / market
        ▼                                         ▼
03_market.sql  ── stg_odds  (no-vig probabilities, join_key)
               ── market_lines  1 row / odds line, + match_status, P(over)
        ▼
04_eval.sql    ── bet_results, clv_summary, calibration_buckets
```

How it runs. `src/pipeline.py` does five things in order:

1. Opens DuckDB.
2. Registers Python functions so SQL can call them: `normalize_name`,
   `normalize_team`, `canonical_name`, `build_key` and `normal_cdf`.
3. Creates the two `raw_*` views. These are the only place file paths appear.
4. Runs `sql/01_*.sql` through `04_*.sql` in order.
5. Runs `CHECKS`: queries that must return 0, such as duplicate counts and
   row-count differences.

Three ideas run through the whole project. If you remember only three things,
remember these:

1. **Grain.** Every table has a comment saying what one row means. Most bugs
   are grain bugs: a join that turns 1 row into 2 (fan-out), or that drops rows.
2. **No leakage.** A feature for game *t* may only use games before *t*. The
   window frame `ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING` is how we enforce it.
3. **Never guess identity.** When a name and team could mean two players, or
   none, the line is labelled (`ambiguous_name`, `no_player`, ...) and left out.
   It is never forced to match.

---

## 2. Guided tour

Read each file top to bottom, then answer the questions without looking back.

### `src/resolve.py` (Python, but it shapes the SQL)

- `normalize_name("Amon-Ra St. Brown")` gives `"amonrastbrown"`, and
  `"Kenneth Walker III"` gives `"kennethwalker"`.
- `build_key(name, team)` gives `"name|TEAM"`. Both the stats side (01) and the
  odds side (03) build keys with this one function, so the keys line up.

<details><summary>Q1. Why is the team part of the key, and not just the name?</summary>

Two different players can share a name: there were two Mike Williams in the
NFL. Adding the team separates most of them. It also lets a traded player have
one key per team, which matches how sportsbooks list players.
</details>

<details><summary>Q2. What does an unknown team turn into, and why doesn't it match anything?</summary>

`build_key` writes `?` for the team, giving `djmoore|?`. In `03_market.sql`
both lookups require `o.join_key NOT LIKE '%|?'`. "Unknown" is not a team, so
two unknowns on different sides must not count as the same team. A test
(`test_unknown_team_on_both_sides_is_never_matched`) caught exactly this bug
before it was fixed.
</details>

### `sql/01_staging.sql`

<details><summary>Q3. Name the two CTEs in stg_player_week and say what each one is for.</summary>

`reg_season_skill` filters rows: regular season, and QB/RB/WR/TE only.
`cleaned` fixes types, turns NULLs into 0 with `coalesce`, renames
`recent_team` to `team`, and adds `week_index`, `name_key` and `join_key`.
</details>

<details><summary>Q4. Why doesn't this file remove duplicate rows?</summary>

If the source ever has two rows for the same player-week, that's a data
problem we want to *see*. A `DISTINCT` would hide it. Instead the pipeline
check `stg_player_week_dupes` and the test `test_stg_one_row_per_player_week`
fail loudly.
</details>

<details><summary>Q5. Why is week_index = season*100 + week? Why not just sort by week?</summary>

The project spans several seasons. 2023 week 18 is 202318 and 2024 week 1 is
202401, so one integer sorts correctly across seasons. `02` uses it as the
`ORDER BY` for a `RANGE` window, which needs a single numeric sort key.
</details>

<details><summary>Q6. What is the grain of player_name_xwalk, and why does a traded player get 2 rows?</summary>

The grain is one row per (player_id, season, join_key). The key includes the
team, so Marquise Brown on ARI and on KC is two different keys. A book lists a
player under the team they're on at game time, so we need both.
</details>

### `sql/02_features.sql`

<details><summary>Q7. Read the WINDOW last6 clause aloud in plain English.</summary>

"For each player, line up their games in date order. For the current game,
look at the up-to-6 games *before* it, not including this game itself."
`PARTITION BY` = per player. `ORDER BY` = date order. `ROWS` = count rows
(games), not weeks. `... AND 1 PRECEDING` = stop one row before the current
one.
</details>

<details><summary>Q8. A player misses week 3 (bye). In week 4, how many prior games are in the window? Why?</summary>

Weeks 1 and 2: 2 games. `ROWS` counts rows, and there's no row for a game that
wasn't played. Under `RANGE` with `week_index` the frame would be defined by
week *values*, not rows. See `test_window_counts_games_not_weeks`.
</details>

<details><summary>Q9. Why UNION ALL four SELECTs to make player_projections?</summary>

It turns one wide row (all stats) into up to four long rows (one per market),
each with its own position filter, so there are no QB receiving props. DuckDB's
`UNPIVOT` could do this, but the UNION ALL makes the filters obvious. Use
`UNION ALL`, not `UNION`: `UNION` also removes duplicates, which wastes time
and could hide a bug.
</details>

<details><summary>Q10. The model_sd window uses RANGE, not ROWS. What would go wrong with ROWS?</summary>

The partition is per *market*, so many players share the same week. With
`ROWS ... 1 PRECEDING`, a row could see other players' rows from the **same
week** that happen to sort before it. That's leakage: those games haven't
finished when you bet. `RANGE ... 1 PRECEDING` on `week_index` excludes every
row with the current week's value. Spot-the-bug **B** below breaks exactly
this, and 10 tests fail.
</details>

### `sql/03_market.sql`

<details><summary>Q11. What's a MACRO, and what do american_to_prob(-110) and american_to_prob(+150) return?</summary>

A macro is a reusable SQL expression, like a small function. −110 gives
110/210 = 0.524. +150 gives 100/250 = 0.400. Prices strictly between −100 and
+100 return NULL, because they aren't valid American odds.
</details>

<details><summary>Q12. What does "no-vig" mean? Compute it for over −130 / under +110.</summary>

The over's implied probability is 130/230 = 0.5652, and the under's is
100/210 = 0.4762. Together they sum to 1.0414; the extra 0.0414 is the book's
margin (the vig). Divide each by the sum: the no-vig over is
0.5652 / 1.0414 = **0.5427**. (Exercise 4.3 does this in SQL.)
</details>

<details><summary>Q13. Why is market_lines guaranteed to have exactly one row per odds line?</summary>

Every join is a `LEFT JOIN`, so no odds row is dropped. Each right-hand table
is unique on its join columns:

- `season_keys` is `SELECT DISTINCT`.
- `week_keys` is a `GROUP BY` on season, week and join_key.
- `player_projections` is unique per player, week and market.

So no row can be duplicated either. The `market_fanout` check and
`test_market_join_has_no_fanout` prove it.
</details>

<details><summary>Q14. Why match odds to the player's game in that same week, not just the season?</summary>

Two reasons:

1. **Team at game time.** A line saying "McCaffrey, SF" in a week he played
   for CAR shouldn't silently match his CAR game.
2. **No identity leakage.** If a second player with the same name joins the
   team later, a season-level lookup would turn an *earlier* line ambiguous.
   That means the future changed the past.

Matching per week also mirrors the sportsbook rule: a prop on a player who
doesn't play is void.
</details>

<details><summary>Q15. List the match_status values in the order they're checked. Why does the order matter?</summary>

`bad_odds` → `no_player` → `no_game` → `ambiguous_name` →
`insufficient_history` → `matched`. A `CASE` returns the first true branch.
A row with bad odds *and* an unknown player should report the most basic
problem first.
</details>

### `sql/04_eval.sql`

<details><summary>Q16. How does the model pick a side?</summary>

Bet the over if the model's P(over) ≥ the market's no-vig P(over) at the open.
Otherwise bet the under. `edge_over` is that difference.
</details>

<details><summary>Q17. Define CLV in points for an over bet and for an under bet. Why is clv_prob sometimes NULL?</summary>

- **Over:** `close_line − open_line`. The line rising means the market agreed
  with you.
- **Under:** `open_line − close_line`.

`clv_prob` compares no-vig probabilities at open and close, which only makes
sense when both are for the *same* line. When the line moved, it's NULL.
</details>

<details><summary>Q18. What does a calibration bucket tell you? What does gap > 0 mean?</summary>

A bucket groups the bets the model said had, say, a 60–70% chance of going
over. If the model is honest, about 65% of them should go over. `gap` =
actual over rate − average predicted probability. A positive gap means overs
hit *more* often than the model said, so it was too pessimistic.
</details>

---

## 3. Exercises

Write each query yourself, run it with `scripts/q.py`, then compare.

### Level 1: SELECT, WHERE, ORDER BY, GROUP BY, HAVING

**1.1** How many rows does `stg_player_week` have per season?

<details><summary>Answer</summary>

```sql
SELECT season, count(*) AS n_rows
FROM stg_player_week
GROUP BY season
ORDER BY season;
```
You get about 5,200–5,300 rows per season: 5298, 5252, 5294 and 5227.
</details>

**1.2** The 5 best single-game receiving performances of 2023: name, team,
week and yards.

<details><summary>Answer</summary>

```sql
SELECT player_display_name, team, week, receiving_yards
FROM stg_player_week
WHERE season = 2023
ORDER BY receiving_yards DESC
LIMIT 5;
```
Amari Cooper had 265 in week 16. DJ Moore had 230 in week 5.
</details>

**1.3** Top 5 teams by total rushing yards in 2024. This only counts our
filtered QB/RB/WR/TE rows; what would that miss?

<details><summary>Answer</summary>

```sql
SELECT team, sum(rushing_yards) AS rush_yds
FROM stg_player_week
WHERE season = 2024
GROUP BY team
ORDER BY rush_yds DESC
LIMIT 5;
```
BAL is first with about 3,189. It misses rushing by other positions, like
fullbacks or a punter on a fake. That's a reminder that staging *filtered*
the data.
</details>

**1.4** Every player-season with at least 1,500 receiving yards.

<details><summary>Answer</summary>

```sql
SELECT player_display_name, season, sum(receiving_yards) AS rec_yds
FROM stg_player_week
GROUP BY player_id, player_display_name, season
HAVING sum(receiving_yards) >= 1500
ORDER BY rec_yds DESC;
```
`WHERE` filters rows *before* grouping, and `HAVING` filters groups *after*.
You group by `player_id` because names aren't unique. The top row is Cooper
Kupp in 2021 with 1,947.
</details>

### Level 2: grain checks, CTEs, joins

**2.1** Prove that `stg_player_week` has one row per player-week. Write a
query that should return **zero rows**.

<details><summary>Answer</summary>

```sql
SELECT player_id, season, week, count(*) AS n
FROM stg_player_week
GROUP BY player_id, season, week
HAVING count(*) > 1;
```
This is the most useful query pattern in data work. Run it on every table
whose grain you claim.
</details>

**2.2** Using `player_name_xwalk`, find players who played for more than one
team in a season. Show the path, like `TEN -> SEA`.

<details><summary>Answer</summary>

```sql
SELECT player_display_name, season,
       count(DISTINCT team) AS n_teams,
       string_agg(team, ' -> ' ORDER BY first_week) AS path
FROM player_name_xwalk
GROUP BY player_id, player_display_name, season
HAVING count(DISTINCT team) > 1
ORDER BY season, player_display_name;
```
Adrian Peterson in 2021 went TEN -> SEA.
</details>

**2.3** Compute each player's **target share**: their targets divided by
their team's total targets that week. Build the team totals in a CTE, join
them back, and then **prove the join didn't change the row count**.

<details><summary>Answer</summary>

```sql
WITH team_week AS (
    SELECT season, week, team, sum(targets) AS team_targets
    FROM stg_player_week
    GROUP BY season, week, team          -- unique per (season, week, team)
),
share AS (
    SELECT s.player_display_name, s.season, s.week, s.team, s.targets,
           s.targets / nullif(t.team_targets, 0) AS target_share
    FROM stg_player_week AS s
    JOIN team_week AS t
      ON t.season = s.season AND t.week = s.week AND t.team = s.team
)
SELECT (SELECT count(*) FROM share)           AS rows_after,
       (SELECT count(*) FROM stg_player_week) AS rows_before;
```
Both counts are 21,071. The join is safe because `team_week` is unique on
the join columns.

`nullif(x, 0)` turns a 0 into NULL, so you get NULL instead of a
divide-by-zero.

Bonus: this would make a good new feature for `02`. Can you add a leak-free
`avg_target_share_l6`?
</details>

**2.4** How many fake odds lines matched? Group `market_lines` by
`match_status`. Then explain why `bet_results` has exactly that many rows.

<details><summary>Answer</summary>

```sql
SELECT match_status, count(*) FROM market_lines GROUP BY match_status;
```
On real stats it's 10 matched and 2 no_player. `bet_results` is built with
`WHERE match_status = 'matched'`, so it has 10 rows. The fixture tests expect
9/1/1/1 because the fixture's made-up stats differ from reality; see
`tests/fixtures/README.md`.
</details>

### Level 3: window functions (the core of this project)

**3.1** For Amon-Ra St. Brown in 2023, show each game's receiving yards, the
previous game's yards, and the change. Use `lag()`.

<details><summary>Answer</summary>

```sql
SELECT player_display_name, week, receiving_yards,
       lag(receiving_yards) OVER (PARTITION BY player_id ORDER BY season, week) AS prev_game,
       receiving_yards
         - lag(receiving_yards) OVER (PARTITION BY player_id ORDER BY season, week) AS change
FROM stg_player_week
WHERE player_display_name = 'Amon-Ra St. Brown' AND season = 2023
ORDER BY week;
```
Week 1's `prev_game` is NULL *within this filtered view*. But `WHERE` runs
before the window, so 2022 rows are gone; drop the `season = 2023` filter and
week 1 would see his last 2022 game. That's a big gotcha.

After week 4 the next row is week 6: he didn't play in week 5, and `lag`
just takes the previous *row*, not the previous week.
</details>

**3.2** Write a **leak-free** 3-game rolling average of targets.

<details><summary>Answer</summary>

```sql
SELECT player_display_name, season, week, targets,
       avg(targets) OVER (
           PARTITION BY player_id
           ORDER BY season, week
           ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING
       ) AS avg_targets_l3
FROM stg_player_week
WHERE player_display_name = 'Amon-Ra St. Brown' AND season = 2023
ORDER BY week;
```
Week 2 shows 9.0, which is week 1's 9 targets. It never includes the current
game.
</details>

**3.3** CeeDee Lamb's 2023 season-to-date receiving yards, two ways: one
*including* the current game and one *excluding* it. Which one could you use
as a feature?

<details><summary>Answer</summary>

```sql
SELECT player_display_name, week, receiving_yards,
       sum(receiving_yards) OVER (PARTITION BY player_id, season ORDER BY week
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS ytd_incl,
       sum(receiving_yards) OVER (PARTITION BY player_id, season ORDER BY week
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS ytd_before
FROM stg_player_week
WHERE player_display_name = 'CeeDee Lamb' AND season = 2023
ORDER BY week;
```
Only `ytd_before` is safe as a feature. `ytd_incl` includes the game you're
trying to predict.

Note `PARTITION BY player_id, season`: this one deliberately resets each
season. `02` does *not* reset, and the comment there explains why.

If you leave out the frame clause and keep `ORDER BY`, the default frame is
`RANGE UNBOUNDED PRECEDING AND CURRENT ROW`, which *includes* the current row.
Defaults are leakage traps, so always write the frame out.
</details>

**3.4** For each week of 2024, the WR with the most targets. Use `rank()` and
`QUALIFY`.

<details><summary>Answer</summary>

```sql
SELECT season, week, player_display_name, team, targets
FROM stg_player_week
WHERE season = 2024 AND position = 'WR'
QUALIFY rank() OVER (PARTITION BY season, week ORDER BY targets DESC) = 1
ORDER BY week;
```
`QUALIFY` filters on a window result, the way `HAVING` filters on an aggregate.

Ties both show up: in week 2, St. Brown and Nabers each had 18. With
`row_number()` you'd get one of them arbitrarily.
</details>

### Level 4: evaluate the model

**4.1** For each market, compute the mean absolute error (MAE) and the bias
(mean residual) of the projections.

<details><summary>Answer</summary>

```sql
SELECT market,
       count(residual)               AS n,
       round(avg(abs(residual)), 2)  AS mae,
       round(avg(residual), 2)       AS bias
FROM player_projections
GROUP BY market
ORDER BY market;
```
For rec_yds the MAE is about 19.2 yards and the bias is about −0.4. `count(residual)`
skips NULLs, so `n` only counts rows that had a projection.
</details>

**4.2** For `rec_yds`, is the projection more accurate with 6 prior games than
with 3–5? Compare MAE. Then explain the surprise.

<details><summary>Answer</summary>

```sql
SELECT CASE WHEN n_prior_games < 6 THEN '3-5' ELSE '6' END AS history,
       round(avg(abs(residual)), 2) AS mae,
       count(*) AS n
FROM player_projections
WHERE market = 'rec_yds' AND residual IS NOT NULL
GROUP BY 1
ORDER BY 1;
```
The 3–5 group has the *lower* error: about 17.0 against 19.5. More history
didn't help. That's **selection**, not a better model. Players with short
histories are mostly backups who catch few passes, and small numbers make
small errors. To compare fairly you'd scale the error by volume, for example
MAE divided by average yards. Always ask "who is in each group?"
</details>

**4.3** Do Q12 in SQL: implied probabilities, vig and no-vig over for
−130/+110.

<details><summary>Answer</summary>

```sql
WITH p AS (SELECT 130.0/230 AS imp_over, 100.0/210 AS imp_under)
SELECT imp_over, imp_under,
       imp_over + imp_under - 1             AS vig,
       imp_over / (imp_over + imp_under)    AS nv_over
FROM p;
```
The no-vig over is 0.5427. Use `130.0`, not `130`: in some SQL engines
integer ÷ integer truncates to 0. DuckDB returns a decimal either way, but
the habit protects you elsewhere.
</details>

**4.4 (stretch)** Add a `roi_by_side` breakdown: group `bet_results` by
`market, side`, with bet count, win rate excluding pushes, and total profit.
There's no answer key for this one. Check it by hand against `bet_results`.

---

## 4. Spot the bug

Each snippet is a one-line change to the real SQL. For each one, say:

- (a) what goes wrong,
- (b) whether it's leakage, fan-out, dropped rows or wrong logic,
- (c) which test catches it.

Every one was verified by applying the change and running the suite.

**A.** `sql/02_features.sql`, window `last6`:
```sql
ROWS BETWEEN 5 PRECEDING AND CURRENT ROW
```
<details><summary>Answer</summary>

**Leakage.** The average now includes the game being predicted, so the
projection "knows" the answer. 14 tests fail, including
`test_rolling_avgs_match_manual_strictly_earlier_games` and the mutation tests
in `test_adversarial.py`.
</details>

**B.** `sql/02_features.sql`, window `prior_weeks`:
```sql
ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
```
<details><summary>Answer</summary>

**Leakage.** It's subtle: see Q10. Other players' games from the same week
seep into `model_sd`. 10 tests fail, including
`test_mutating_every_stat_of_every_player_in_week_t_changes_nothing_at_or_before_t`.
The existing single-player mutation test also catches it, because it checks
`model_sd` for every player.
</details>

**C.** `sql/03_market.sql`:
```sql
    JOIN week_keys AS wk          -- was LEFT JOIN
```
<details><summary>Answer</summary>

**Dropped rows.** An inner join throws away every line with no game that
week, so `market_lines` no longer has one row per odds line. The
`market_fanout` check (which compares counts) fails the pipeline itself, so
most tests error.

Lesson: a "fan-out" check that compares row counts catches drops too.
</details>

**D.** `sql/01_staging.sql`, delete the line
`AND position IN ('QB', 'RB', 'WR', 'TE')`.
<details><summary>Answer</summary>

**Wrong logic or grain.** Kickers and other positions leak into staging, and
the row counts no longer match. 6 tests fail, including
`test_stg_excludes_post_and_non_skill_positions` and the count tests.
</details>

**F.** `sql/03_market.sql`, delete `AND o.join_key NOT LIKE '%|?'` (both
lines).
<details><summary>Answer</summary>

**Wrong logic.** Two unknown teams now match each other. Only 1 test catches
it: `test_unknown_team_on_both_sides_is_never_matched`. It was written by the
testing agent specifically because the original code had this bug. Edge
cases need their own tests.
</details>

**G.** `sql/03_market.sql`, delete `AND wk.week = o.week` from the
`week_keys` join.
<details><summary>Answer</summary>

**Fan-out.** Each odds line now matches *every* week the player played that
season, about 17 copies. `market_fanout` and the `odds_id` duplicate checks
fail.
</details>

**H.** `sql/01_staging.sql`:
```sql
CAST(receiving_yards AS DOUBLE) AS receiving_yards   -- coalesce removed
```
<details><summary>Answer</summary>

**Wrong logic, and quiet.** The real data has no NULLs today, so nothing
visibly breaks. But a NULL would make `avg()` skip that game. A player would
look like they averaged their *other* games' yards, when they really had 0.
Only `test_stg_nulls_become_zero` catches it, using a NaN planted in the
fixture.
</details>

**E (discussion).** `sql/04_eval.sql`, `graded` CTE:
```sql
            ELSE 'push'          -- was: WHEN actual = open_line THEN 'push'
```
<details><summary>Answer</summary>

**No test fails.** This is the interesting one. `bet_results` only sees
`matched` rows, and `matched` already guarantees a non-NULL `open_line` and
`actual`. So the `ELSE` branch can never be reached today, and the strict
version is *defence in depth*.

Should you still write it strictly? Yes. If `03` ever changes, the strict
version leaves a NULL `result` for missing data. The `bet_results_ungraded`
check then fails loudly, instead of the bad row silently counting as a push.

Exercise: write a test that *would* catch E. Hint: you can't through the
pipeline. You'd have to test the `graded` logic on a hand-made row.
</details>

---

## 5. Rewrite drills

The tests are your grader. For each drill:

1. Save the original: `cp sql/01_staging.sql /tmp/01_original.sql`.
2. Clear the file: `: > sql/01_staging.sql`.
3. Write it from scratch, using only `PLAN.md` §2.3 and this guide.
4. Run the tests for that file, listed below, until they're green.
5. Compare with `git diff sql/01_staging.sql`. Differences are fine if the
   tests pass. Ask yourself whether yours is clearer.
6. Restore if you like: `git checkout sql/01_staging.sql`.

| Drill | Rewrite | Spec | Grader |
|---|---|---|---|
| 1 | `sql/01_staging.sql` | PLAN.md §2.3 | `pytest tests/test_staging.py -q` |
| 2 | `sql/02_features.sql` | PLAN.md §2.4 | `pytest tests/test_features_leakage.py -q` |
| 3 | `sql/03_market.sql` | PLAN.md §2.5 | `pytest tests/test_market_eval.py -q` |
| 4 | `sql/04_eval.sql` | PLAN.md §2.6 | `pytest tests/test_market_eval.py -q` |
| Final | all four | — | `make test` (all 112) |

Tips:

- Each file depends on the ones before it, so rewrite in order and keep the
  earlier ones working.
- The table and column names must match exactly, because tests query them by
  name. Check `PLAN.md` for the list.
- When a test fails, read its assertion first; it tells you the expected
  value. Then run the same query yourself with the fixture data. A quick way:
  `pytest tests/test_staging.py -q -x --pdb`, then run `con.sql("...")` at the
  prompt.
- Start ugly. Get the test green, *then* refactor into named CTEs with
  comments.

### Mini-drills (15 minutes each)

1. Write the `WINDOW last6` clause from memory, then explain each of its 4
   parts out loud.
2. Write `american_to_prob` as a `CASE` expression from memory.
3. Write a "must return zero rows" grain check for `player_projections`.
   Its grain: player_id, season, week, market.
4. Write the `match_status` `CASE` from memory, in the right order.
5. Write `calibration_buckets` from memory. Why `least(..., 0.9)`?

---

## 6. Glossary

| Term | Meaning here |
|---|---|
| **Grain** | What one row of a table represents. Write it in a comment above every table. |
| **CTE** | `WITH name AS (SELECT ...)`: a named step inside a query. One step per idea. |
| **Fan-out** | A join that turns 1 row into several because the right side isn't unique on the join keys. |
| **Window function** | `f(...) OVER (PARTITION BY ... ORDER BY ... frame)`: computes across related rows *without* collapsing them the way `GROUP BY` does. |
| **ROWS vs RANGE** | `ROWS` counts physical rows. `RANGE` uses the `ORDER BY` *values*, so rows tied on that value are all in or all out. |
| **Leakage** | A feature that uses information you wouldn't have had when betting. The fastest way to fool yourself. |
| **NULL logic** | `NULL = NULL` is NULL, not TRUE. `avg`, `count(col)` and `stddev` skip NULLs. `count(*)` doesn't. |
| **Implied probability** | What a price implies, vig included. |
| **No-vig / fair probability** | Implied probabilities rescaled to sum to 1. |
| **CLV** | Closing line value: did the market move your way after you bet? It's the most trusted sign of real edge. |
| **Calibration** | Do 60% predictions come true about 60% of the time? |
| **Push** | Exact tie on a whole-number line. The stake comes back. |

---

## 7. Suggested two-week plan

| Day | Do |
|---|---|
| 1 | §0 setup, §1 big picture. Read `01_staging.sql`; questions Q1–Q6. |
| 2 | Exercises 1.1–1.4 and 2.1. |
| 3 | Exercises 2.2–2.4. Write a grain check for every table. |
| 4 | Read `02_features.sql`; Q7–Q10. |
| 5 | Exercises 3.1–3.4. Re-do 3.3 until the default-frame trap makes sense. |
| 6 | Spot-the-bug A, B, D, H. |
| 7 | **Drill 1:** rewrite `01_staging.sql`. |
| 8 | **Drill 2:** rewrite `02_features.sql`. |
| 9 | Read `03_market.sql`; Q11–Q15; exercise 4.3. |
| 10 | Spot-the-bug C, F, G. **Drill 3:** rewrite `03_market.sql`. |
| 11 | Read `04_eval.sql`; Q16–Q18; exercises 4.1, 4.2. |
| 12 | **Drill 4:** rewrite `04_eval.sql`. Spot-the-bug E. |
| 13 | Exercise 4.4, and add `avg_target_share_l6` to `02` with a leakage test. |
| 14 | Final drill: all four files from blank, `make test` green. |

Next real-world step: replace `FAKE_odds.csv` with real historical closing
lines, same columns. Then the CLV and calibration tables start meaning
something.
