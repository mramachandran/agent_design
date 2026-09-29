# Prompt: Standout-First Discovery

Build the "where should I look?" experience for the Peak insights map.
Marketers don't know which place has a story, so the product leads with a
**ranked Top stories list** and uses the map to explore it. Every story is
precomputed per data release, judged, and served from Redis.

This prompt extends `MAP_INSIGHTS_PROMPT.md` and `SCORE_BASED_PRECOMPUTE_PROMPT.md`.
Where they conflict, this file wins for scoring, story types, and caching.

---

## 1. Goals and non-goals

**Goals**
- Surface the 5–10 most pitch-worthy places without the user searching.
- Every story shows its comparison baseline ("+48% vs national +31%").
- Never present statistical noise as a story.
- Every read path (list, map layer, place panel, CSV) is served from cache.

**Non-goals**
- No LLM calls on hover, list load, filter change, or export.
- No changes to chat routing. "Ask about this place" only pre-fills the chat.

---

## 2. Story types

Each place can carry zero or more stories. A story = type + value + baseline +
plain-English label.

| Type | Label shown | Value | Baseline | Fires when (all must hold) |
|---|---|---|---|---|
| `fastest_growth` | Fastest growth | yoy_pct vs aligned 2025 day(s) | peer median yoy_pct | percentile >= NOTABLE_PCTL **and** value - baseline >= GROWTH_MIN_GAP_PTS (default 10) |
| `rank_jump` | Biggest rank jump | rank_2025 - rank_2026 | none (absolute) | jump >= RANK_JUMP_MIN[level] (state 3, city 5, zip 25) |
| `cm_surge` | Cyber Monday surge | Cyber Monday spike % | peer median spike | percentile >= NOTABLE_PCTL **and** gap >= CM_MIN_GAP_PTS (default 15) |
| `peak_day_moved` | Peak day moved | busiest-day offset in days vs aligned 2025 busiest day | none | offset >= PEAK_DAY_MIN_SHIFT (default 3) |
| `mix_shift` | Delivery mix shift | HD share 2026 - 2025 (pts) | peer median shift | percentile >= NOTABLE_PCTL **and** gap >= MIX_MIN_GAP_PTS (default 3) |
| `state_outlier` | Outlier in its state | yoy_pct minus same-state median | state median | gap >= STATE_OUTLIER_MIN_GAP_PTS (default 15) |

Rules for all types:
- All YoY values use the holiday-aligned views (`v_yoy_state_daily`,
  `v_peak_date_alignment`). Never a blind +1 year shift.
- Drop a story if the prior-year baseline volume < MIN_BASELINE (50) or the
  2026 forecast volume < MIN_VOLUME[level] (config).
- `mix_shift` is behind `ENABLE_MIX_SHIFT_STORIES` (default **false**) until
  the product-code mapping is verified. When off, it is neither computed nor shown.
- Peers = same level. Cities are compared within volume tier
  (`CITY_VOLUME_TIERS` config, default 3 tiers) so small cities aren't
  compared with metros.

---

## 3. Scoring (SQL, Tier A: every place, no LLM)

### 3.1 Shrinkage (small-place noise guard)
Before percentiles, shrink each rate toward its peer median:

```
shrunk = (n * value + K * peer_median) / (n + K)
```

- `n` = prior-year baseline volume, `K` = SHRINK_K (config, default 200).
- Percentiles and gaps use `shrunk`, not raw `value`.
- Raw value is still shown in the UI; `shrunk` only drives ranking.

### 3.2 Percentile
- Per type, per level (and per volume tier for cities): percentile of
  `|shrunk - peer_median|` across peers.

### 3.3 Standout score
- `standout_score` = max percentile across the place's firing stories (0-100).
- `top_insights` = up to 3 firing stories, highest percentile first.
- No firing story → `standout_score = 0`, `top_insights = []`, headline
  "Tracks the national pattern". This is a valid, expected outcome.

### 3.4 Rank score (what orders the Top stories list)
```
rank_score = (1 - VOLUME_WEIGHT) * standout_score
           + VOLUME_WEIGHT * volume_percentile
```
- `VOLUME_WEIGHT` default 0.3, so a big place with a good story beats a tiny
  place with a slightly better one.
- Tie-break: higher volume, then slug (deterministic).

### 3.5 Diversity in the list
When building the ranked list, enforce:
- at most `MAX_PER_STATE` (default 2) places from one state in the top N
- at most `MAX_PER_TYPE_SHARE` (default 0.4) of the top N sharing one primary
  story type
Skipped places stay in the full ranking; they just don't crowd the top list.

### 3.6 Empty-year guard
If fewer than `MIN_STORIES_FOR_LIST` (default 5) places fire at a level, show
what exists and say "Few places stand out at this level this year."

---

## 4. Headlines and briefs

- **Template headline (Tier A, no LLM):** built from the top insight, e.g.
  `"Cyber Monday surge: +48% vs national +31%"`. <= 110 chars. Always
  includes the baseline.
- **Tier B/C briefs:** LLM narration from stored facts only (reuse
  `prompts/press_brief.md`, add `top_insights`), then the judge
  (grounded, time_frame, honest). Store only on PASS; one retry; on failure
  keep the template headline and log to `insight_failures.json`.
- Tier B (precomputed) selection, on-demand Tier C path, lock, and rate
  limit: unchanged from `SCORE_BASED_PRECOMPUTE_PROMPT.md`.
- Every brief states its time frame and the matched comparison day(s), e.g.
  "2026 forecast (Nov 27 - Dec 24) vs matched 2025 days".

---

## 5. Caching (everything cached, versioned)

### 5.1 Keys

| Key | Value | Notes |
|---|---|---|
| `insight:{level}:{slug}:{V}` | full place object (stats, stories, headline, brief, judge verdict) | per place |
| `insight_layer:{level}:{V}` | compact array for the map (no briefs) | state < 20 KB, city < 500 KB |
| `insight_layer:zip:{state}:{V}` | ZIP layer for one state | < 500 KB |
| `stories:{level}:{type\|all}:{V}` | ordered list of top `STORIES_MAX` (default 100) `{slug, name, state, type, headline, rank_score, volume}` | powers the Top stories list |
| `stories_csv:{level}:{type\|all}:{V}` | pre-rendered CSV text | export = cache read |
| `lock:{key}` | `1`, `SET NX EX 60` | stampede guard for on-demand briefs |

`V = {data_version}:{SCORING_VERSION}:{PROMPT_VERSION}`
- `data_version` changes on each data release.
- `SCORING_VERSION` is a constant bumped whenever thresholds, shrinkage, or
  weights change, so scoring changes never serve stale rankings.
- `PROMPT_VERSION` is bumped when narration or judge prompts change; it only
  needs to invalidate `brief`/`headline` fields but is kept in `V` for simplicity.

### 5.2 Layers
- **L1:** in-process LRU (layers and story lists), short TTL (60 s).
- **L2:** Redis.
- **Browser:** layer and stories endpoints return `ETag = V` and
  `Cache-Control: public, max-age=300`; `If-None-Match` → 304. Gzip on.
- Filters (state, min volume, story type) are applied **client-side** to the
  cached story list where possible. Server-side filtered requests use a
  hashed key `stories:{level}:{filters_hash}:{V}` with a 1 h TTL.

### 5.3 Invalidation
- New `data_version` or bumped `SCORING_VERSION` → new keys; old keys deleted
  after a successful build (never before).
- `python -m jobs.build_place_insights --purge` → deletes all `insight*`,
  `stories*` keys for the current version.
- `INCR cache_epoch` supported as an emergency purge (epoch included in the
  layer/stories keys if the existing global cache already uses it).
- `X-No-Cache: 1` header (or `CACHE_DISABLED=true`) bypasses reads for eval
  and debugging; never writes a bypassed result.

### 5.4 Failure behavior
- Redis down → serve L1 if present, else return a clear 503 with retry hint.
  Never fall through to live BigQuery or LLM calls from these endpoints.

---

## 6. API

| Endpoint | Returns |
|---|---|
| `GET /api/stories?level=state\|city\|zip&type=all\|fastest_growth\|...&state=PA&min_volume=N&limit=25` | ranked stories (from cache) |
| `GET /api/stories.csv?level=...&type=...` | CSV export (from cache) |
| `GET /api/map/layer?level=...&state=PA` | map layer |
| `GET /api/map/place?level=...&slug=...` | full place object, on-demand brief per Tier C rules |

- ZIP layer/stories require `state` (400 otherwise).
- Cache miss on a list/layer → 404 with "not built for this data version",
  never a live rebuild.
- All endpoints parameterized; no user text interpolated into SQL or keys
  (slugs validated against `^[a-z0-9-]+$`).

---

## 7. Frontend (`demo-chat-app/map/`)

Layout: **Top stories list (left) + map (right) + place panel (slide-over)**.

### Top stories list
- Default view on load: level = State, type = All, top 10.
- Each row: rank, place name, story-type chip, headline (with baseline),
  volume. Click → fly to place and open the panel.
- Filter chips: story type (multi-select), level toggle, min volume slider,
  state selector.
- Keyboard: arrow keys move through rows, Enter opens, `/` focuses search.
  This is also the accessibility path for the map.

### Map
- Color = `standout_score` by default (single-hue, 5–7 steps, same in light and
  dark). Toggle: Standout · Volume · YoY % · Cyber Monday spike.
- Gray + hatch for places below MIN_BASELINE / no data.
- Hover: name, template headline (no LLM), and a "why is this red?" line
  naming the top story and its baseline.
- Hovering a list row highlights the place on the map and vice versa.

### Place panel
- Full brief, top stories with baselines, matched comparison day(s).
- Buttons:
  - **Copy for press**: headline + brief + source line ("2026 forecast,
    Nov 27 - Dec 24; compared with matched 2025 days").
  - **Copy headline**
  - **Export list (CSV)** from the list header.
  - **Ask about this place**: pre-fills chat with
    "What's special about {place} this Peak?"; that question resolves to the
    cached brief through place resolution, not `sql_fallback`.
- When `brief_status = generating`, show stats + template summary and poll.

### Honest states
- No stories: "Nothing unusual here. {place} tracks the national pattern."
- Below thresholds: "Not enough prior-year volume to compare."
- Level with few stories: message from section 3.6.

---

## 8. Tests

**Scoring**
- Shrinkage: a 20-package place with +300% raw does not outrank a
  5,000-package place with +45%.
- Absolute gap: when all peers are within 2 pts, nothing fires even though a
  top 5% exists.
- MIN_BASELINE and MIN_VOLUME drops; `mix_shift` off by default.
- Volume-tier peers for cities.
- Diversity caps (per state, per type) applied to the list, not the ranking.
- Empty-year path and few-stories path.

**Caching**
- Second request is a cache hit (no BigQuery, no LLM).
- Bumping `SCORING_VERSION` produces new keys; old keys cleaned only after a
  successful build.
- `--purge` clears the current version; `X-No-Cache` bypasses without writing.
- Redis down → L1 or 503, never a live query.
- Concurrent on-demand clicks generate once (lock).
- ETag / 304 works on `/api/stories` and `/api/map/layer`.

**API / UI**
- ZIP without state → 400; invalid slug → 400.
- CSV matches the list order and count.
- Payload size limits from section 5.1.
- Judge failure → template headline stored, nothing cached as a brief.
- Copy-for-press text includes time frame and matched-day line.

---

## 9. Build order (stop for review after each)

1. **Scoring SQL + dry run.** `--dry-run --level state --limit 10` printout:
   place, story types, raw vs shrunk value, baseline, percentile,
   rank_score, template headline. Also print counts of firing stories per type
   per level. No Redis writes yet.
2. **Cache + API.** Story lists, CSV, layers, place endpoint, versioned keys,
   purge, ETag.
3. **Top stories list UI** (no map yet), with filters and keyboard nav.
4. **Map** with linked hover/highlight and color toggle.
5. **Briefs.** Tier B batch, Tier C on demand, judge gate.

---

## 10. Constraints

- Reuse the existing Redis client, BigQuery executor, byte cap, judge, and
  place resolver. Do not add a new cache service.
- Thresholds live in one config module; no magic numbers in SQL or UI code.
- Show SQL and diffs before creating or replacing any view.
- No employer or company names in code, comments, docs, or sample data.
- Keep the free-text chat and current routing unchanged.
