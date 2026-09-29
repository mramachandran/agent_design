# Prompt: Standout-First Discovery (v2)

Build the "where should I look?" experience for the Peak insights map.
Marketers don't know which place has a story, so the product leads with a
**ranked Top stories list** and uses the map to explore it. Every story is
precomputed per data release, judged, and served from Redis.

This prompt extends `MAP_INSIGHTS_PROMPT.md` and `SCORE_BASED_PRECOMPUTE_PROMPT.md`.
Where they conflict, this file wins for scoring, story types, caching and API.

**v2 changes (after review):** per-type sign and score source; honest
baseline labels (no "national" unless it is the volume-weighted national
figure); same-ZIP-set YoY and coverage guard; 2024 rebound check; forecast
artifact flags; minimum peer-group sizes; uncapped rankings with caps only on
the default view; atomic active-version pointer; prompt version out of the
stats keys; content-hash ETags and private caching; admin-only cache bypass;
ZIP disclosure guard; CSV formula escaping; press-draft wording; feedback
logging; numeric acceptance criteria per build step.

---

## 1. Goals and non-goals

**Goals**
- Surface the 5–10 most pitch-worthy places per level without the user searching.
- Every story shows its comparison and names what the comparison is.
- Never present statistical noise, coverage artifacts, or forecast quirks as a story.
- Every read path (list, map layer, place panel, CSV) is served from cache.

**Non-goals**
- No LLM calls on hover, list load, filter change, or export.
- No changes to chat routing. "Ask about this place" only pre-fills the chat
  and goes through the existing press-brief place resolution.
- No approval workflow in v1 (see section 8, "Press wording").

---

## 2. Definitions

| Term | Meaning |
|---|---|
| Peak window | Nov 27 – Dec 24, 2026 (forecast); matched 2025 and 2024 days via `v_peak_date_alignment` |
| volume | total forecast packages for the place over the Peak window |
| baseline volume | matched-day actual packages for the same place, 2025 |
| yoy_pct | (volume − baseline volume) / baseline volume, on the **same ZIP set** (section 3.1) |
| level | `state`, `city` (city + state), `zip` |
| peers | same level; cities and ZIPs additionally within a volume tier (section 3.3) |
| national_yoy_pct | volume-weighted national YoY on the same ZIP set; stored once per data release as a fact |
| data_version | identifier of the data release the numbers came from; shown to users as "Forecast as of {date}" |

All YoY values use the holiday-aligned views. Never a blind +1 year shift.

---

## 3. Data guards (apply before any scoring)

### 3.1 Same-ZIP-set comparison
- Compute every YoY number on the intersection of ZIPs that (a) exist in both
  years and (b) match the geo reference. Otherwise a change in match rate
  between years shows up as growth or decline.
- Store `coverage_pct` per place = matched volume / total forecast volume for
  that place. Show it in the place panel footnote.
- Suppress all stories for a place when `coverage_pct < COVERAGE_MIN`
  (default 70) or when coverage differs by more than `COVERAGE_MAX_DRIFT_PTS`
  (default 5) between 2026 and 2025. Stats are still shown, with the reason.

### 3.2 Minimum volumes
| | state | city | zip |
|---|---|---|---|
| `MIN_BASELINE` (2025 matched actuals) for story eligibility | 5,000 | 500 | 200 |
| `MIN_VOLUME` (2026 forecast) for appearing on the map at all | 1,000 | 100 | 50 |

Config, per level. The dry run (section 10, step 1) is where these get tuned.

### 3.3 Volume tiers
- Cities and ZIPs are split into `VOLUME_TIERS` (default 3) by forecast
  volume, per level. Percentiles and peer medians are computed within a tier.
- A type is skipped for a place when its peer group has fewer than
  `MIN_PEERS` (default 20) members. State medians (for `state_outlier`) need
  at least `MIN_PLACES_PER_STATE` (default 5) places, otherwise skip.

### 3.4 Forecast artifact flags
A story is tagged `concentrated` (and excluded from the default Top stories
view, still visible via filter) when:
- more than `CONCENTRATION_MAX_SHARE` (default 40%) of the YoY delta comes
  from a single day, or
- more than the same share comes from a single product code.
The panel says why: "Most of this growth is forecast for one day (Dec 1)".

### 3.5 Rebound check (2024)
- For `fastest_growth`, `cm_surge` and `state_outlier`, also compute the same
  value against aligned 2024. If the 2026 vs 2025 gap holds but 2026 vs 2024
  does not (direction flips or gap < half the threshold), tag the story
  `rebound` instead: "+48% vs 2025, but roughly flat vs 2024".
- `rebound` stories stay in the list but rank below non-rebound stories with
  the same score, and the headline uses the rebound wording.

### 3.6 ZIP disclosure guard
- If a shipper/account dimension exists in the source data: a ZIP story is
  eligible only when the ZIP has at least `MIN_SHIPPERS` (default 10) distinct
  shippers in the baseline year and the top shipper is at most
  `MAX_TOP_SHIPPER_SHARE` (default 50%) of volume.
- If no such dimension exists: raise ZIP `MIN_BASELINE` to 1,000 and never
  allow "Copy for press" at ZIP level (section 8). Say which case applies in
  the dry-run output.

---

## 4. Story types

Each place can carry zero or more stories. A story = type + value + baseline
+ baseline label + plain-English label. All growth-type stories are
**one-sided (upward)**; declines are not stories in v1 but remain visible on
the map through the YoY color toggle.

| Type | Label shown | Value (raw) | Baseline and its label | Score source | Fires when (all must hold) |
|---|---|---|---|---|---|
| `fastest_growth` | Fastest growth | yoy_pct | peer median yoy_pct — "median of {peer label}" | one-sided percentile of `shrunk − baseline` within peers | pctl ≥ NOTABLE_PCTL and `shrunk − baseline` ≥ GROWTH_MIN_GAP_PTS (10) |
| `cm_surge` | Cyber Monday surge | CM spike % as defined by the existing `cyber_monday_spike` handler | peer median spike — "median of {peer label}" | one-sided percentile of `shrunk − baseline` | pctl ≥ NOTABLE_PCTL and gap ≥ CM_MIN_GAP_PTS (15) |
| `rank_jump` | Biggest rank jump | state: rank_2025 − rank_2026 (positions); city/zip: percentile-rank change (pts) | none — "vs its 2025 rank" | percentile of the jump within peers | jump ≥ RANK_JUMP_MIN[level] (state 3 positions; city 10 pts; zip 10 pts) |
| `peak_day_moved` | Peak day moved | signed days between 2026 busiest day and aligned 2025 busiest day | none — "vs matched 2025 busiest day" | percentile of `abs(offset)` within peers | `abs(offset)` ≥ PEAK_DAY_MIN_SHIFT (3); headline says "earlier" or "later" |
| `mix_shift` | Delivery mix shift | HD share 2026 − 2025 (pts) | peer median shift — "median of {peer label}" | one-sided percentile of `shrunk − baseline` | pctl ≥ NOTABLE_PCTL and gap ≥ MIX_MIN_GAP_PTS (3); behind `ENABLE_MIX_SHIFT_STORIES` (default **false**) |
| `state_outlier` | Leading its state | yoy_pct − same-state median yoy_pct | state median — "median of {n} {cities/ZIPs} in {state}" | one-sided percentile of the gap within the state | city and zip levels only; gap ≥ STATE_OUTLIER_MIN_GAP_PTS (15); state has ≥ MIN_PLACES_PER_STATE |

Peer label = "all states", "similar-size cities", "similar-size ZIPs in {state}".

Rules:
- `NOTABLE_PCTL` = 90 (config). The legend and docs must quote the same number.
- A story never says "national" unless the number is `national_yoy_pct`.
  The national figure is shown as secondary context everywhere:
  `+48% (median of similar-size cities +31%; national +12%)`.
- Drop a story if the place fails any guard in section 3.
- `state_outlier` is suppressed when `fastest_growth` already fires for the
  same place (they measure the same thing); it becomes a secondary tag on the
  growth story instead: "…and leading Pennsylvania".

---

## 5. Scoring (SQL, Tier A: every place, no LLM)

### 5.1 Shrinkage (small-place noise guard)
Rates (`yoy_pct`, CM spike, HD share shift) are shrunk toward the peer median
before percentiles:

```
shrunk = (n * value + K * peer_median) / (n + K)
```

- `n` = baseline volume; `K = SHRINK_K[level]` (config; starting points
  state 0, city 500, zip 1,000).
- The dry run prints, per level and volume tier, the spread of 2024→2025
  growth. Set K so that a place at the 10th percentile of volume needs
  roughly twice the gap of a place at the 90th percentile to fire. Record the
  chosen K and the reason in the config file.
- Rank-based values (`rank_jump`) are not shrunk; the percentile-rank
  definition handles scale.
- The UI shows the raw value; `shrunk` only drives eligibility and ranking.

### 5.2 Percentiles
- Per type, per level, per volume tier: one-sided percentile of
  `shrunk − peer_median` (or of the magnitude, for `rank_jump` and
  `peak_day_moved`) across peers.
- Skip the type when the peer group is below `MIN_PEERS`.

### 5.3 Standout score
- `standout_score` = percentile of the place's **primary story** (the firing
  story with the highest percentile). 0–100.
- The number of firing stories is displayed ("2 stories") but does not raise
  the score.
- `top_insights` = up to 3 firing stories, highest percentile first.
- No firing story → `standout_score = 0`, `top_insights = []`, headline
  "Tracks the national pattern". Expected for most places.
- Dry run reports the share of places with ≥ 2 firing stories per level; if
  it exceeds 30%, thresholds are too loose.

### 5.4 Rank score (orders the list)
```
rank_score = (1 − VOLUME_WEIGHT) * standout_score
           + VOLUME_WEIGHT * volume_percentile
```
- `VOLUME_WEIGHT` default 0.3.
- **Only places with at least one firing, non-suppressed story enter the
  ranking.** A place with `standout_score = 0` never appears in a story list.
- `rebound` and `concentrated` stories rank after non-tagged stories with the
  same `rank_score`.
- Tie-break: higher volume, then slug.

### 5.5 Full ranking vs default view
- The job stores the **full, uncapped** ranking per level (ZIPs per state).
- The **default view** (level = state/city, type = all, no filters) applies
  diversity caps on top of the ranking:
  - at most `MAX_PER_STATE` (default 2) places per state (city and zip levels only)
  - at most `MAX_PER_TYPE_SHARE` (default 0.4) of the shown rows with the
    same primary type
- Filtered views (state, type, min volume) read the full ranking and apply
  **no** caps.

### 5.6 Empty-year guard
If fewer than `MIN_STORIES_FOR_LIST` (default 5) places fire at a level, show
what exists with "Few places stand out at this level this year."

---

## 6. Headlines and briefs

- **Template headline (Tier A, no LLM):** from the primary story. ≤ 110 chars.
  Always includes the baseline **and its label**, e.g.
  `Cyber Monday surge: +48% vs +31% median of similar-size cities`.
  Rebound variant: `Growth +48% vs 2025 (flat vs 2024)`.
- Every headline carries a "Forecast" badge in the UI; the copied text starts
  with "Forecast:".
- **Tier B/C briefs:** LLM narration from stored facts only (reuse
  `prompts/press_brief.md`, add `top_insights`, `national_yoy_pct`,
  `coverage_pct`, 2024 comparison, tags), then the judge (grounded,
  time_frame, honest). Store only on PASS; one retry; on failure keep the
  template headline and log to `insight_failures.json`.
- Add a judge check: the brief must not contain the word "national" unless
  the national figure is in the facts and matches.
- Tier B selection, on-demand Tier C path, lock and rate limit: unchanged from
  `SCORE_BASED_PRECOMPUTE_PROMPT.md`, except the brief stores its own
  `prompt_version`; a brief whose `prompt_version` differs from the current
  one is treated as missing (regenerated by Tier B batch or lazily by Tier C).
- Every brief states: time frame, "forecast", matched comparison days, the
  baseline label, and "Forecast as of {data_version date}".

---

## 7. Caching (everything cached, versioned)

### 7.1 Version pointer
- `V = {data_version}:{SCORING_VERSION}` — data release plus a constant bumped
  whenever thresholds, shrinkage, tiers or weights change.
- `PROMPT_VERSION` is **not** part of V. It lives on the brief fields only
  (section 6), so a prompt tweak never invalidates stats, layers or lists.
- The job writes everything under the new V, then sets
  `active_version:{level}` = V in one atomic write, **last**. The API reads
  the pointer on every request (L1-cached for 10 s) and never derives V from
  code constants.
- A deploy with a bumped `SCORING_VERSION` keeps serving the old V until the
  job finishes and flips the pointer. No 404 window.
- Old V keys are deleted `OLD_VERSION_GRACE_MIN` (default 30) minutes after a
  successful flip, never before.

### 7.2 Keys

| Key | Value | Notes |
|---|---|---|
| `active_version:{level}` | V | pointer; written last |
| `insight:{level}:{slug}:{V}` | full place object (stats, stories, tags, coverage, headline, brief + prompt_version, judge verdict) | per place |
| `insight_layer:state:{V}`, `insight_layer:city:{V}` | compact array for the map (no briefs) | state < 20 KB, city < 500 KB |
| `insight_layer:zip:{state}:{V}` | ZIP layer for one state | < 500 KB |
| `ranking:state:{V}`, `ranking:city:{V}` | full uncapped ranking `{slug, name, state, primary_type, types[], tags[], headline, rank_score, standout_score, volume, yoy_pct}` | eligible places only |
| `ranking:zip:{state}:{V}` | same, one state | |
| `stories:{level}:default:{V}` | default view after diversity caps, top `STORIES_MAX` (100) | precomputed by the job |
| `stories:{level}:{filters_hash}:{V}` | filtered view, no caps | computed on first request from `ranking:*`, TTL 1 h |
| `stories_csv:{level}:{default\|filters_hash}:{V}` | pre-rendered CSV text | export = cache read |
| `facts:national:{V}` | `national_yoy_pct`, national CM spike, data_version date | shown as context |
| `lock:{key}` | `1`, `SET NX EX 60` | stampede guard for on-demand briefs |

Filter hash = sha1 of the canonicalized filter set (sorted keys, lowercase
values). Slugs validated against `^[a-z0-9-]+$`; state codes against
`^[A-Z]{2}$`.

### 7.3 Layers
- **L1:** in-process LRU for pointer, layers, rankings and default views,
  TTL 60 s (pointer 10 s).
- **L2:** Redis.
- **Browser:** `ETag` = sha1 of the response body; `Cache-Control: private,
  max-age=300`; `If-None-Match` → 304. Gzip on. Never `public`: the data is
  an unpublished forecast.

### 7.4 Invalidation
- New `data_version` or bumped `SCORING_VERSION` → new V (section 7.1).
- `python -m jobs.build_place_insights --rebuild` → full build under the same
  V with a temporary suffix, then pointer flip, then cleanup. There is no
  "delete now" purge in normal operation.
- `INCR cache_epoch` (if the existing global cache already uses it) is
  included in the L1 key only, as an emergency L1 flush; Redis keys are
  governed by V.
- **Cache bypass** (`X-No-Cache: 1` or `CACHE_DISABLED=true`): honored only
  for identities in `ADMIN_EVAL_IDENTITIES` (checked against the
  IAP-asserted identity header). For everyone else the header is ignored.
  Bypass on `/api/map/place` still goes through the lock and the on-demand
  rate limit. Bypass on list/layer endpoints has no effect (there is no live
  path) and the response says so in a header.

### 7.5 Failure behavior
- Redis down → serve L1 if present, else 503 with `Retry-After`. Never fall
  through to live BigQuery or LLM calls from these endpoints.

---

## 8. API

| Endpoint | Returns |
|---|---|
| `GET /api/stories?level=state\|city\|zip&type=all\|fastest_growth\|...&state=PA&min_volume=N&include_tagged=0\|1&limit=25` | ranked stories. No filters → default view (caps). Any filter → uncapped filtered view |
| `GET /api/stories.csv?...same params` | CSV export of the same rows |
| `GET /api/map/layer?level=...&state=PA` | map layer |
| `GET /api/map/place?level=...&slug=...` | full place object; on-demand brief per Tier C rules |
| `GET /api/facts/national` | national context + "Forecast as of" |
| `POST /api/stories/feedback` | `{level, slug, action: copied_headline\|copied_press\|thumbs_up\|thumbs_down\|asked_chat}` |

- `level=zip` requires `state` (400 otherwise). Invalid slug or state → 400.
- Cache miss on pointer → 503 "not built yet"; miss under a valid pointer →
  404 "not in this data version". Never a live rebuild.
- All responses include `data_version`, `scoring_version`, `forecast_as_of`.
- CSV: `Content-Disposition: attachment`; any cell starting with `=`, `+`,
  `-`, `@`, tab or CR is prefixed with `'`. Row order and count equal the
  JSON response for the same params.
- Feedback is appended to a `story_feedback` table (or Redis stream if no
  table exists) with identity, timestamp, V. Reviewed weekly to tune
  thresholds; no PII beyond the asserted identity.

**Press wording (v1, no approval workflow)**
- The button is labeled **"Copy draft for press"**. The copied text begins:
  `DRAFT — based on the 2026 Peak forecast as of {date}; not for external
  release until approved by the communications team.` followed by headline,
  brief, comparison line and baseline label.
- Not available at ZIP level (section 3.6). At ZIP level the button reads
  "Copy summary (internal)" and omits the press framing.
- Every copy is logged via the feedback endpoint.

---

## 9. Frontend (`demo-chat-app/map/`)

Layout: **Top stories list (left) + map (right) + place panel (slide-over)**.
Header shows "Forecast as of {date}" and the national context line.

### Top stories list
- Default on load: level = State, type = All, top 10, tagged stories hidden.
- Each row: rank, place name, story-type chip (plus `rebound` /
  `concentrated` tag chips when shown), headline with baseline label, volume,
  "Forecast" badge.
- Filters: story type (multi-select), level toggle, min volume slider, state
  selector, "show rebound/concentrated" toggle. Any filter → uncapped view;
  the header says "Top by score (no per-state limit)".
- Keyboard: arrows move, Enter opens, `/` focuses search. This is the
  accessibility path for the map.

### Map
- Color = `standout_score` by default (single-hue, 5–7 steps, same in light
  and dark). Toggle: Standout · Volume · YoY % · Cyber Monday spike.
- Gray + hatch for places below `MIN_VOLUME`, failing coverage, or no data;
  hover explains which.
- Hover: name, template headline (no LLM), and "why is this red?" naming the
  primary story, its baseline and baseline label.
- Hovering a list row highlights the place and vice versa.

### Place panel
- Full brief, top stories with baselines and labels, matched comparison days,
  2024 comparison, `coverage_pct` footnote, tags with their explanation.
- Buttons: **Copy draft for press** (not at ZIP), **Copy headline**,
  **Ask about this place** (pre-fills "What's special about {place} this
  Peak?" and uses the existing press-brief place resolution), thumbs up/down.
- **Export list (CSV)** in the list header.
- `brief_status = generating` → stats + template summary, poll every 3 s up
  to 30 s.

### Honest states
- No stories: "Nothing unusual here. {place} tracks the national pattern."
- Guard failure: "Not enough prior-year volume to compare" / "Geo coverage
  too low ({coverage_pct}%) to compare reliably".
- Few stories at a level: message from section 5.6.

---

## 10. Build order and acceptance criteria (stop for review after each)

1. **Scoring SQL + dry run.** `--dry-run --level all --limit 10`. No Redis
   writes. Output must include:
   - per level and type: count of firing stories, median / p90 / p99 of the
     raw and shrunk gaps, number of places skipped for `MIN_PEERS`
   - per level: share of places with ≥ 2 stories; share of top-10 with
     baseline below the 25th volume percentile
   - the 10 top places per level with: types, raw vs shrunk value, baseline
     and label, percentile, rank_score, tags, coverage_pct, template headline
   - which ZIP disclosure case applies (section 3.6)
   - **Accept when:** 5–15 non-tagged stories per level for city and state;
     ≤ 20% of any top-10 comes from the bottom volume quartile; no headline
     contains "national" except via `national_yoy_pct`.
2. **Cache + API.** Pointer, rankings, default and filtered views, CSV,
   layers, place endpoint, facts, feedback, ETag.
   - **Accept when:** cache-hit p95 < 100 ms for list and layer endpoints;
     the deploy-before-job test (section 11) passes; payload limits hold.
3. **Top stories list UI** with filters and keyboard nav.
   - **Accept when:** a state filter returns more than `MAX_PER_STATE` rows
     when they exist; every row shows a baseline label and Forecast badge.
4. **Map** with linked hover/highlight and color toggle.
   - **Accept when:** no network call or LLM call fires on hover; legend
     quotes `NOTABLE_PCTL`.
5. **Briefs.** Tier B batch, Tier C on demand, judge gate, prompt_version
   handling.
   - **Accept when:** judge pass rate ≥ 80% on Tier B; a prompt-version bump
     leaves stats and lists untouched and regenerates briefs.

---

## 11. Tests

**Guards and scoring**
- Same-ZIP-set: a ZIP present only in 2026 does not create growth.
- Coverage: a place at 65% coverage has stats but no stories; a place whose
  coverage moved 8 pts between years has no stories, with the reason.
- Shrinkage formula: with K = 500, n = 600, value = 300, median = 30 →
  shrunk ≈ 177. (Formula test, not an ordering claim.)
- Ordering acceptance (dry-run assertion): a place with baseline just above
  `MIN_BASELINE` and raw gap of 2× threshold does **not** outrank a place at
  the top volume tier with raw gap of 1.5× threshold.
- One-sided: a place at −40% never fires `fastest_growth`.
- Absolute gap: when all peers are within 2 pts, nothing fires even though a
  top 10% exists.
- `MIN_PEERS`: a tier with 12 members produces no percentile stories.
- `state_outlier` skipped in a state with 3 cities; suppressed when
  `fastest_growth` fires for the same place.
- Rebound: +40% vs 2025 and −2% vs 2024 → tagged `rebound`, ranks below an
  untagged story with equal `rank_score`.
- Concentrated: 55% of the delta on one day → tagged, hidden from default view,
  visible with `include_tagged=1`.
- `mix_shift` off by default. ZIP disclosure guard in both cases of section 3.6.
- Eligibility: `standout_score = 0` never appears in any story list.

**Rankings and views**
- Default view respects both diversity caps; filtered view (state=PA) returns
  every eligible PA place up to `limit`, ignoring caps.
- Type filter ignores the per-type share cap.
- ZIP list without state → 400.

**Caching**
- Deploy-before-job: bump `SCORING_VERSION`, restart API, do **not** run the
  job → all endpoints still serve the previous V.
- Pointer flip is atomic: readers see either old or new V, never a mix.
- Old V keys survive the grace period and are deleted after it.
- Prompt-version bump: stats/layers/lists unchanged; briefs regenerate.
- ETag changes when a brief lands within the same V; 304 otherwise.
- `Cache-Control` is `private` on every endpoint.
- Bypass header ignored for non-admin identity; for admin identity on
  `/api/map/place` it still respects lock and rate limit.
- Redis down → L1 or 503, never a live query.
- Concurrent on-demand clicks generate once (lock).

**API / UI / export**
- CSV cells starting with `=`, `+`, `-`, `@` are escaped; order and count
  match JSON.
- Copy-draft text begins with the DRAFT line and includes time frame,
  "forecast", matched days, baseline label and "as of" date.
- No Copy-draft button at ZIP level.
- Feedback endpoint stores action, identity, V; rejects unknown actions.
- Payload limits from section 7.2.
- Judge failure → template headline stored, nothing cached as a brief.
- Judge rejects a brief that says "national" when the fact is a peer median.

---

## 12. Constraints

- Reuse the existing Redis client, BigQuery executor, byte cap, judge, place
  resolver and `cyber_monday_spike` definition. Do not add a new cache service.
- Thresholds, K values, tiers and caps live in one config module with a
  comment per value explaining how it was chosen; no magic numbers in SQL or
  UI code.
- Show SQL and diffs before creating or replacing any view.
- No employer or company names in code, comments, docs, or sample data.
- Keep the free-text chat and current routing unchanged.
