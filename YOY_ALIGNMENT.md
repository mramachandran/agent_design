# Holiday-Aligned Year-over-Year Comparison

Implementation spec + coding-assistant prompt + reference SQL for comparing a
holiday peak-season **forecast** against **prior-year actuals** on matching days.

Replace placeholders before use:

| Placeholder | Meaning |
|---|---|
| `{PROJECT}` | Warehouse project / catalog |
| `{DATASET}` | Dataset / schema holding the views |
| `forecast_daily` | Forecast table/view: `delivery_date, zip, city, state, product, daily_volume` |
| `actuals_daily` | Actuals table/view: `actual_date, zip, product, actual_volume` |

---

## 1. Why alignment matters

Daily volume follows **weekday** and **holiday position**, not calendar date.
Comparing the same calendar date across years compares different weekdays
(e.g. a Monday against a Sunday) and misaligns movable holidays such as
Thanksgiving, Black Friday and Cyber Monday. A blind `+1 YEAR` shift produces
misleading year-over-year swings.

## 2. Alignment rule

| Compare year | Shift | Effect |
|---|---|---|
| Prior year (Y-1) | minus **364 days** (52 weeks) | Same weekday; movable holidays line up |
| Two years back (Y-2) | minus **728 days** (104 weeks) | Same weekday; movable holidays line up |

**Christmas exception.** Christmas is fixed-date. If the current date is
**Dec 23 or later**, or the shifted date lands on **Dec 24 or 25** of the prior
year, use the **same calendar date** instead.

The alignment is computed in SQL. The LLM only extracts the current-year date
(or holiday) and which year(s) to compare against — it never does date math.

### Expected results (2026 season — use as test assertions)

| 2026 date | 2025 match | 2024 match | Rule |
|---|---|---|---|
| Nov 27 (Black Friday) | Nov 28 | Nov 29 | weekday_aligned |
| Nov 30 (Cyber Monday) | Dec 1 | Dec 2 | weekday_aligned |
| Dec 14 (Mon) | Dec 15 | Dec 16 | weekday_aligned |
| Dec 22 | Dec 23 | Dec 22 | 2025 weekday / 2024 calendar |
| Dec 24 | Dec 24 | Dec 24 | christmas_calendar |

---

## 3. Coding-assistant prompt

Paste into Claude Code, Copilot, or similar.

```text
Implement holiday-aligned year-over-year (YoY) comparison for a
natural-language data agent.

CONTEXT
- Forecast covers the current peak season (Nov 27 – Dec 24, 2026) in
  {PROJECT}.{DATASET}.forecast_daily.
- Prior-year actuals (2025 and 2024 peaks) are in
  {PROJECT}.{DATASET}.actuals_daily.
- The existing YoY view uses a blind +1 YEAR shift. This compares different
  weekdays and misaligns Thanksgiving, Black Friday and Cyber Monday. Fix it.

ALIGNMENT RULE (in SQL, never in the LLM)
- 2025 match = 2026 date minus 364 days; 2024 match = minus 728 days.
- Christmas exception: if the 2026 date is Dec 23 or later, OR the shifted
  date lands on Dec 24/25 of the prior year, use the same calendar date.
- Test assertions:
    2026-11-27 -> 2025-11-28, 2024-11-29
    2026-11-30 -> 2025-12-01, 2024-12-02
    2026-12-14 -> 2025-12-15, 2024-12-16
    2026-12-22 -> 2025-12-23, 2024-12-22
    2026-12-24 -> 2025-12-24, 2024-12-24

TASKS
1. SQL — use the reference SQL in YOY_ALIGNMENT.md section 4 as the starting
   point. First inspect actuals_daily and confirm real column names and
   product labels; do not assume.
   a. v_peak_date_alignment
   b. v_actuals_yoy_comparison (rebuilt; pre-aggregate both sides; LEFT JOIN
      actuals)
   c. v_yoy_state_daily (state x day rollup; % from summed volumes; exclude
      placeholder states)

2. Intent router (the Gemini/LLM intent classifier):
   - Add response_schema fields: compare_years (ARRAY of INTEGER) and
     holiday (enum: thanksgiving, black_friday, small_business_saturday,
     cyber_monday, christmas_eve).
   - In the intent rules for yoy_trend: "last year"/"last peak"/no year
     -> [2025]; "2024"/"two years ago" -> [2024]; "past peaks"/"trend"
     -> [2025, 2024]. Return named dates as 2026 dates; do NOT convert them
     to prior-year dates. Comparisons between two dates in the SAME season
     stay sql_fallback.

3. Handler yoy_trend():
   - Accept compare_years (default [2025]) and holiday; map holiday to its
     2026 date.
   - One ZIP -> daily series; one date -> top movers with
     HAVING SUM(prev_volume) >= 50; neither -> national daily series from
     v_yoy_state_daily.
   - Dates outside Nov 27 – Dec 24 -> "no forecast for that date".
     Thanksgiving (Nov 26) -> suggest Black Friday or Cyber Monday.
   - Return prev_date and align_rule so narration can say
     "vs Mon Dec 15, 2025".
   - Use the existing query executor and byte cap. Use query parameters,
     never string-formatted user values.

4. Tests:
   - Unit test the alignment assertions above against v_peak_date_alignment.
   - Add routing eval cases:
     "How are peak numbers year-over-year on Dec 14?" -> yoy_trend
     "How does Cyber Monday compare to 2024?"          -> yoy_trend
     "How do past peaks compare to this year's forecast?" -> yoy_trend
     "How did volume change from Dec 1 to Dec 14?"     -> sql_fallback
     "Was Dec 20 busier than Dec 13?"                  -> sql_fallback

5. Docs: update the project README/agent notes, the YoY metric definition
   shown in the UI, and any agent instructions that say only one prior year
   is available.

CONSTRAINTS
- Do not change other intents or routing rules.
- Show the SQL and the diff before running CREATE OR REPLACE VIEW.
- After changes, run the routing eval for the yoy_trend category and report
  results.
```

---

## 4. Reference SQL (BigQuery)

### 4.1 Date alignment map

```sql
CREATE OR REPLACE VIEW `{PROJECT}.{DATASET}.v_peak_date_alignment` AS
WITH years AS (
  SELECT compare_year, shift_days
  FROM UNNEST([
    STRUCT(2025 AS compare_year, 364 AS shift_days),
    STRUCT(2024 AS compare_year, 728 AS shift_days)
  ])
),
shifted AS (
  SELECT
    cur_date,
    compare_year,
    DATE_SUB(cur_date, INTERVAL shift_days DAY) AS weekday_match
  FROM UNNEST(GENERATE_DATE_ARRAY('2026-11-27', '2026-12-24')) AS cur_date
  CROSS JOIN years
),
flagged AS (
  SELECT
    *,
    -- Christmas anchor: final two days, or a shift landing on Dec 24/25
    cur_date >= DATE '2026-12-23'
      OR (EXTRACT(MONTH FROM weekday_match) = 12
          AND EXTRACT(DAY FROM weekday_match) >= 24) AS use_calendar
  FROM shifted
)
SELECT
  cur_date,
  FORMAT_DATE('%a', cur_date) AS cur_weekday,
  compare_year,
  IF(use_calendar,
     DATE(compare_year, EXTRACT(MONTH FROM cur_date), EXTRACT(DAY FROM cur_date)),
     weekday_match) AS prev_date,
  IF(use_calendar, 'christmas_calendar', 'weekday_aligned') AS align_rule
FROM flagged;
```

### 4.2 ZIP × product YoY comparison

```sql
CREATE OR REPLACE VIEW `{PROJECT}.{DATASET}.v_actuals_yoy_comparison` AS
WITH forecast AS (
  SELECT
    delivery_date,
    zip,
    product,
    ANY_VALUE(city)  AS city,
    ANY_VALUE(state) AS state,
    SUM(daily_volume) AS cur_volume
  FROM `{PROJECT}.{DATASET}.forecast_daily`
  GROUP BY delivery_date, zip, product
),
actuals AS (
  SELECT
    actual_date,
    zip,
    product,
    SUM(actual_volume) AS prev_volume
  FROM `{PROJECT}.{DATASET}.actuals_daily`
  -- filter early: only prior-year peak windows can ever match
  WHERE actual_date BETWEEN '2024-11-28' AND '2024-12-24'
     OR actual_date BETWEEN '2025-11-27' AND '2025-12-24'
  GROUP BY actual_date, zip, product
)
SELECT
  align.cur_date,
  align.cur_weekday,
  align.compare_year,
  align.prev_date,
  align.align_rule,
  fc.zip,
  fc.city,
  fc.state,
  fc.product,
  fc.cur_volume,
  act.prev_volume,
  SAFE_DIVIDE(fc.cur_volume - act.prev_volume, act.prev_volume) AS yoy_pct_change
FROM `{PROJECT}.{DATASET}.v_peak_date_alignment` AS align
JOIN forecast AS fc
  ON fc.delivery_date = align.cur_date
LEFT JOIN actuals AS act                      -- keep forecast rows with no prior actual
  ON act.actual_date = align.prev_date
 AND act.zip         = fc.zip
 AND act.product     = fc.product;
```

### 4.3 State × day rollup

```sql
CREATE OR REPLACE VIEW `{PROJECT}.{DATASET}.v_yoy_state_daily` AS
SELECT
  cur_date,
  cur_weekday,
  compare_year,
  prev_date,
  align_rule,
  state,
  SUM(cur_volume)  AS cur_volume,
  SUM(prev_volume) AS prev_volume,
  -- sum first, then divide: volume-weighted %
  SAFE_DIVIDE(SUM(cur_volume) - SUM(prev_volume), SUM(prev_volume)) AS yoy_pct_change
FROM `{PROJECT}.{DATASET}.v_actuals_yoy_comparison`
WHERE state IS NOT NULL
  AND state NOT IN ('TBD')                    -- placeholder states from geo lookup
GROUP BY cur_date, cur_weekday, compare_year, prev_date, align_rule, state;
```

### 4.4 Handler query patterns

```sql
-- One date: top YoY movers (min-baseline guardrail)
SELECT zip, city, state, compare_year, prev_date, align_rule,
       SUM(cur_volume) AS cur_volume, SUM(prev_volume) AS prev_volume,
       SAFE_DIVIDE(SUM(cur_volume) - SUM(prev_volume), SUM(prev_volume)) AS yoy_pct_change
FROM `{PROJECT}.{DATASET}.v_actuals_yoy_comparison`
WHERE cur_date = @day AND compare_year IN UNNEST(@years)
GROUP BY 1, 2, 3, 4, 5, 6
HAVING SUM(prev_volume) >= @min_base
ORDER BY yoy_pct_change DESC
LIMIT @lim;

-- Whole season: national daily series
SELECT cur_date, cur_weekday, compare_year, prev_date,
       SUM(cur_volume) AS cur_volume, SUM(prev_volume) AS prev_volume,
       SAFE_DIVIDE(SUM(cur_volume) - SUM(prev_volume), SUM(prev_volume)) AS yoy_pct_change
FROM `{PROJECT}.{DATASET}.v_yoy_state_daily`
WHERE compare_year IN UNNEST(@years)
GROUP BY 1, 2, 3, 4
ORDER BY compare_year, cur_date;
```

---

## 5. Validation queries

```sql
-- A. Alignment spot-check (must match the table in section 2)
SELECT *
FROM `{PROJECT}.{DATASET}.v_peak_date_alignment`
WHERE cur_date IN ('2026-11-27', '2026-11-30', '2026-12-14', '2026-12-22', '2026-12-24')
ORDER BY cur_date, compare_year;

-- B. Match rate: share of forecast volume that found a prior-year actual.
--    Low coverage means ZIP or product keys don't line up.
SELECT
  compare_year,
  SAFE_DIVIDE(SUM(IF(prev_volume IS NOT NULL, cur_volume, 0)), SUM(cur_volume)) AS matched_share
FROM `{PROJECT}.{DATASET}.v_actuals_yoy_comparison`
GROUP BY compare_year;

-- C. National YoY by aligned day — should look smooth, no wild swings
SELECT cur_date, cur_weekday, compare_year, prev_date,
       SUM(cur_volume) AS cur_volume, SUM(prev_volume) AS prev_volume,
       SAFE_DIVIDE(SUM(cur_volume) - SUM(prev_volume), SUM(prev_volume)) AS yoy_pct_change
FROM `{PROJECT}.{DATASET}.v_yoy_state_daily`
GROUP BY cur_date, cur_weekday, compare_year, prev_date
ORDER BY compare_year, cur_date;

-- D. Product mix by year — large shifts suggest a product-code mapping issue
SELECT EXTRACT(YEAR FROM actual_date) AS yr, product, SUM(actual_volume) AS volume
FROM `{PROJECT}.{DATASET}.actuals_daily`
WHERE actual_date BETWEEN '2024-11-29' AND '2024-12-24'
   OR actual_date BETWEEN '2025-11-28' AND '2025-12-24'
GROUP BY yr, product
ORDER BY yr, product;
```

---

## 6. Extending to a new season

1. Change `GENERATE_DATE_ARRAY` to the new forecast window.
2. Add the new prior year to `years` with its shift (`364 × n`).
3. Update the date windows in the `actuals` CTE and the Christmas-anchor date.
4. Recompute the assertion table in section 2 and rerun validation query A.
