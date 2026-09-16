# Feature Prompt: Conversational Layer for peak-agent-backend

## Context (paste this as-is so the assistant understands the system)

I have a web app called `peak-agent-backend` deployed on Cloud Run (us-central1).
It turns a user's natural-language request into a BigQuery SQL query, runs it,
and returns results.

Current architecture:
- Primary path: template/intent-based SQL generation (keyword/regex or small
  classifier mapping requests to parameterized queries).
- Fallback path: LLM-generated SQL via Vertex AI Gemini (gemini-2.5-flash),
  called using the end user's own OAuth token (not a service account — this
  bypasses an IAM restriction where I can't grant the BigQuery connection's
  service account `roles/aiplatform.user` myself).
- BigQuery execution always runs as the Cloud Run service account `sa-agent-run`,
  regardless of which path generated the SQL.
- Output today: a raw table of query results, no narration, no memory between
  requests.

I want to add four features on top of this, without changing the SQL
generation or execution paths. Implement them as a new layer that sits
between "query results returned" and "response sent to frontend."

---

## Feature 1: Conversation state / session memory

**Goal:** Support multi-turn refinement ("now break that down by region",
"filter to last 30 days") and follow-up question generation, which both
require knowing what was asked/returned before.

**Requirements:**
- Add a session store keyed by `session_id` (generate one per browser session
  if not provided). Use an in-memory dict for local dev, but structure it
  behind an interface (e.g. `ConversationStore`) so it can be swapped for
  Firestore or Redis in prod without touching call sites.
- Store the last N (default 5) turns per session, each turn containing:
  - `question` (original user text)
  - `generated_sql`
  - `result_schema` (column names + types)
  - `result_summary` (row count + first 3-5 rows, NOT the full result set —
    keep this small, it goes into LLM context on every subsequent call)
  - `timestamp`
- When a new request comes in with an existing `session_id`, prepend the
  stored turns (formatted compactly) to the prompt sent to the SQL-generation
  LLM step, so it can resolve references like "that", "it", "the same but
  filtered by X".
- Add a TTL/eviction policy (e.g. sessions expire after 30 min idle) so the
  in-memory version doesn't leak.

**Interface sketch (adjust as needed to fit existing code style):**
```python
class ConversationStore:
    def get_turns(self, session_id: str) -> list[Turn]: ...
    def add_turn(self, session_id: str, turn: Turn) -> None: ...
    def clear(self, session_id: str) -> None: ...
```

---

## Feature 2: Follow-up question suggestions

**Goal:** After returning results, suggest 2-3 natural next questions as
clickable chips in the frontend.

**Requirements:**
- After query execution, make a lightweight LLM call (small/fast model,
  separate from the SQL-gen call) with: the original question, the SQL used,
  and the result summary (schema + sample rows, same shape as stored in
  conversation state above).
- Prompt it to return **strictly JSON**, e.g.:
  ```json
  {"follow_ups": ["Break this down by region", "Compare to last month", "Show only the outliers"]}
  ```
- Add a response schema/validator so malformed LLM output doesn't break the
  API contract — fall back to an empty list on parse failure, log the raw
  response for debugging.
- Return `follow_ups` as part of the existing API response payload (new field,
  don't restructure existing fields).
- When the frontend sends a follow-up chip's text back as a new query, it
  should be treated as a normal new request but MUST include the existing
  `session_id` so Feature 1's context resolution kicks in.

---

## Feature 3: Role-based conversational narration

**Goal:** Alongside the raw result table, generate a short natural-language
summary whose tone/framing depends on a `persona` parameter.

**Requirements:**
- Accept a `persona` field in the request: `"analyst" | "marketing" | "executive"`
  (default `"analyst"` if omitted).
- Maintain a dict/config of system prompts per persona, e.g.:
  - `analyst`: precise, includes deltas/percentages, flags anomalies, comfortable with jargon.
  - `marketing`: benefit/story-framed, minimal raw numbers, emphasizes "what this means."
  - `executive`: one sentence, leads with the single most important number or decision, no methodology.
- Make this a **separate LLM call** from SQL generation and from follow-up
  suggestion generation — keep these three LLM calls independent so any one
  can be cached, retried, or swapped models independently.
- Structure the persona prompts in a config file (e.g. `personas.yaml` or
  `personas.py`) rather than inline strings, so tone can be tuned without
  touching request-handling code.
- Return the narration text as a new `summary` field in the response payload.

---

## Feature 4: Response length control

**Goal:** Let the caller control how long the narration (Feature 3) is.

**Requirements:**
- Accept a `length` field: `"brief" | "standard" | "detailed"`.
- If omitted, infer a sensible default from `persona`
  (executive → brief, analyst → detailed, marketing → standard) rather than
  requiring both params every time.
- Map each length value to both:
  - A `max_tokens` cap on the narration LLM call (hard cap, don't rely on the
    model to self-limit), and
  - An explicit instruction in the prompt (e.g. "Respond in one sentence" /
    "2-3 sentences" / "a short paragraph with specific numbers").
- Keep this orthogonal to persona — same length options should work with any
  persona.

---

## Feature 5: Chart/graph selection

**Goal:** Decide, per result set, whether and how to visualize the data
(line, bar, scatter, pie, KPI card, or plain table), without hardcoding chart
type to query type.

**Requirements:**
- Use a **heuristic-first, LLM-fallback** approach — do not send every result
  to an LLM to pick a chart type; most cases are decidable from the schema
  alone and should cost zero extra latency/tokens.
- Heuristic rules (apply in order, first match wins):
  - 1 row, 1 numeric column → `kpi` (big-number card, no chart)
  - 1 date/time column + 1 numeric column → `line`
  - 1 categorical column (cardinality < 15) + 1 numeric column → `bar`
  - 1 categorical column (cardinality >= 15) + 1 numeric column → `bar`,
    but flag `truncate_top_n: 10` in the response so the frontend shows top
    10 + "other"
  - 2 numeric columns, no categorical/date column → `scatter`
  - date/time column + numeric column + one low-cardinality categorical
    (i.e. a series dimension) → `line` with `series` field set to that column
  - Anything not matched above (3+ dimensions, ambiguous types, mixed
    aggregation levels) → fall through to the LLM step below
- LLM fallback: a separate, small/fast LLM call (same pattern as follow-ups
  and narration — independent, JSON-only, doesn't touch SQL generation).
  Feed it: result schema, 3-5 sample rows, and the original question. Force
  strict JSON output, e.g.:
  ```json
  {"chart_type": "line", "x": "ship_date", "y": "volume", "series": "region"}
  ```
- Constrain `chart_type` to a fixed enum: `line | bar | scatter | pie | kpi | table`.
  Never let the LLM (heuristic or fallback path) emit a type outside this set —
  validate the response and default to `table` if it does or if parsing fails.
- Response payload gets a new `chart_spec` field:
  ```json
  {
    "chart_type": "bar",
    "x": "region",
    "y": "volume",
    "series": null,
    "truncate_top_n": null
  }
  ```
  `chart_type: "table"` means the frontend should just render the existing
  table with no chart — this is a valid and common outcome, not a failure
  case.
- Log which path produced the decision (heuristic rule name, or "llm_fallback")
  so you can see over time how often the LLM path is actually needed and
  tune the heuristics accordingly.
- Unit tests: each heuristic rule with representative schemas, the
  high-cardinality truncation flag, and LLM-fallback JSON parsing/validation
  (including malformed/out-of-enum responses defaulting to `table`).

---

## Feature 6: Caching strategy

**Goal:** Reduce LLM cost/latency and avoid redundant BigQuery execution
across the SQL-gen, follow-up, narration, and chart-selection calls
introduced above.

**Requirements:**

- **Prompt/context caching (LLM calls):** The system prompts, schema
  descriptions, few-shot SQL examples, and persona instructions are static
  across requests — only the user's question and recent conversation turns
  change. Use Vertex AI's context caching for these static portions so each
  call (SQL-gen, follow-ups, narration, chart selection) only pays full
  token cost for the small delta. Structure prompts so the static
  instructional part is a separable prefix/block, not interleaved with
  per-request content, so it's actually cacheable.

- **SQL-generation cache:** Before invoking the LLM fallback path, hash the
  normalized question (lowercased, whitespace-stripped, session-specific
  references removed) and check a cache for a previously generated SQL
  query for that hash. On hit, skip the LLM call entirely and go straight to
  execution. This is separate from and in addition to the existing
  template-matching primary path — it specifically targets repeated
  natural-language phrasings that fall through to the LLM path.

- **BigQuery result cache:** Confirm the existing BigQuery client calls are
  not setting `use_query_cache=False`. BigQuery caches deterministic query
  results for 24 hours by default at no extra cost — verify this is active
  and not being bypassed, before adding any new caching layer on top of it.

- **Response-layer cache (narration / follow-ups / chart spec):** Key cache
  entries on `(sql_hash, persona, length)` for narration, and `(sql_hash)`
  for follow-ups and chart spec (these don't depend on persona/length). On
  a cache hit, skip the corresponding LLM call and return the cached value.

- **Storage:** Use Cloud Memorystore (managed Redis) for the SQL-gen and
  response-layer caches — these need to be shared across Cloud Run
  instances and survive instance restarts, so an in-memory dict (as used for
  the Feature 1 conversation store in local dev) is not sufficient here in
  production. Structure the cache client behind an interface, same pattern
  as `ConversationStore`, so local dev can use an in-memory stub.

- **TTLs (tune later, start with):**
  - SQL-gen cache: 6-24 hours (question phrasing → SQL mapping is stable)
  - Response-layer cache (narration/follow-ups/chart spec): 5-15 minutes
    (underlying data can change; "latest volume" type answers go stale fast)
  - Do not cache anything keyed on relative time phrases (e.g. "today",
    "this week") without including the resolved date range in the cache key,
    or you'll serve stale answers past a day/week boundary.

- **Cache invalidation:** No manual invalidation needed for TTL-based
  expiry, but log cache hit/miss rates per layer (SQL-gen, narration,
  follow-ups, chart spec) so cost/latency savings can be measured and TTLs
  tuned with real data.

- Unit tests: cache key normalization (including relative-date handling),
  hit/miss behavior, and TTL expiry for each cache layer.

---

## Feature 7: Clarifying questions when the request is ambiguous

**Goal:** When the SQL-generation step (template or LLM fallback) can't
confidently map the user's question to a query, ask a targeted clarifying
question instead of guessing or returning an empty/wrong result.

**Requirements:**

- **Detection — build this into the existing SQL-gen step, don't bolt on a
  separate ambiguity check.** When the LLM fallback generates SQL, have it
  also return a confidence/completeness signal alongside the query, e.g.:
  ```json
  {
    "sql": "...",
    "confidence": "low",
    "ambiguity": "time_range",
    "clarifying_question": "Do you mean volume for today, this week, or a specific date range?"
  }
  ```
  If `confidence` is `low` or the LLM can't produce valid SQL at all, return
  the clarifying question to the user instead of executing anything against
  BigQuery.

- **Common ambiguity categories to prompt the LLM to check for explicitly**
  (give it this list in the system prompt so it knows what "ambiguous"
  means in this domain, rather than leaving it to guess):
  - **Missing/unclear time range** — "volume" with no date qualifier
    ("today", "this week", "Q3") and no default the user has established
    in this conversation.
  - **Ambiguous metric** — a term that maps to more than one column/table
    (e.g. "volume" could mean shipment count, weight, or revenue depending
    on the dataset — list the actual candidates from the schema).
  - **Ambiguous entity/dimension** — a name or grouping that matches
    multiple things (e.g. a hub code that's also a region name, or "last
    week" meaning calendar week vs. rolling 7 days).
  - **Aggregation level unclear** — question doesn't specify whether it
    wants a total, a daily breakdown, or per-entity breakdown.
  - **Comparison without a baseline** — "how does this compare" with no
    stated comparison point (previous period? target? another region?).
  - **Out-of-scope / no matching schema** — the question doesn't map to any
    known table/column at all; this should return a "not supported" message
    with a suggestion of what data IS available, not a fabricated query.

- **Response shape:** When a clarifying question is returned, use a
  distinct field so the frontend can render it differently from a normal
  result (e.g. as a chat bubble with quick-reply options, not a table):
  ```json
  {
    "type": "clarification_needed",
    "question": "Do you mean volume for today, this week, or a specific date range?",
    "suggested_options": ["Today", "This week", "Last 30 days"]
  }
  ```
  Populate `suggested_options` when the ambiguity category has a small,
  enumerable set of likely answers (time range, metric choice) — this lets
  the frontend show tappable chips instead of forcing free-text follow-up,
  consistent with the follow-up-question chips from Feature 2.

- **Conversation continuity:** When the user answers a clarifying question,
  it should be treated as a continuation of the same turn, not a new
  independent question — resolve it against the pending ambiguity using the
  session state from Feature 1 (i.e. re-run SQL generation with the original
  question + the clarifying answer appended, not the answer alone).

- **Guardrail:** cap clarification loops at 1 round — if the follow-up
  answer is still ambiguous, fall back to the best-guess SQL with a caveat
  in the narration (e.g. "Assuming this means daily totals — let me know if
  you meant something else") rather than asking a second clarifying
  question. Repeated back-and-forth without a result is a worse experience
  than a labeled best guess.

- Unit tests: each ambiguity category triggers the expected
  `clarification_needed` response (not silent best-guessing), the
  conversation-continuation path correctly merges the original question +
  clarifying answer, and the one-round cap is enforced.

---

## Feature 8: Persistent per-user conversation history (last 20)

**Note:** This is distinct from Feature 1's session state. Feature 1 is
short-lived, per-`session_id`, TTL-evicted (~30 min), and exists to resolve
"that"/"it"-style references within one active back-and-forth. Feature 8 is
long-lived, per-`user_id`, and exists so a user can come back later (new
tab, next day) and see/reuse their past questions — it's a history feature,
not a context-resolution mechanism, though it can feed into one (see below).

**Requirements:**

- **Storage:** Use Firestore (not Memorystore/Redis — this needs to persist
  indefinitely, not just live in cache) with `user_id` as the document key
  or partition. Identify the user from the existing OAuth token used for the
  Gemini fallback call — don't introduce a separate auth mechanism.
- **Retention:** Keep the last 20 turns per user. On each new turn, append
  and trim the oldest if over 20 — a simple bounded list/subcollection, not
  a full unbounded log. If there's ever a need for longer-term analytics
  (e.g. "what do people ask most"), that should be a separate aggregate
  log, not this per-user structure — don't conflate the two.
- **What to store per turn** (same shape as Feature 1's `Turn`, so the two
  can share a data model if convenient):
  - `question`, `generated_sql`, `result_schema`, `result_summary`,
    `persona`, `chart_spec`, `timestamp`
  - Do NOT store full result sets — same reasoning as Feature 1, keep this
    small since it's read on every session start.
- **Retrieval:**
  - Expose an endpoint/method to fetch a user's last 20 turns (for a
    "recent questions" panel in the UI, letting them click to re-run or
    view a past result's SQL/summary again).
  - On session start, optionally hydrate Feature 1's short-term session
    state from the most recent 1-2 stored turns, so a returning user in a
    *new* session can still say "same as what I asked yesterday" and have
    it resolve — this is the one place the two features intentionally
    connect.
- **Privacy/scope:** This is per-user, not per-organization — one user
  should never be able to query or see another user's history. Enforce this
  at the query layer (filter strictly by the authenticated `user_id` from
  the token), not just in the UI.
- **Write path:** Append to Firestore asynchronously after the response is
  already sent to the user — don't block the response on this write.
- Unit tests: trimming behavior at the 20-turn boundary, retrieval scoped
  correctly per user, and that a Firestore write failure doesn't affect the
  main request/response path (fire-and-forget, logged on failure, not
  retried inline).

---

## Non-functional requirements

- Do not modify the existing template-matching or Gemini-fallback SQL
  generation logic — this is purely additive, sitting after SQL execution.
- All new LLM calls (follow-ups, narration) should be resilient to failure:
  if either fails or times out, the response should still return successfully
  with the table data; missing `follow_ups`/`summary` fields should degrade
  gracefully on the frontend, not error out.
- Add logging around each new LLM call (prompt used, latency, token count) so
  cost/latency can be tracked separately from the SQL-gen path.
- Write unit tests for: conversation store eviction, follow-up JSON parsing
  (including malformed responses), persona prompt selection, and length →
  max_tokens mapping.

## Deliverable

Implement this as a new module (suggest `conversation_layer.py` or similar)
with clear separation between the seven concerns (session state, follow-ups,
narration, chart selection, caching, clarification handling, persistent
per-user history), wired into the existing request handler with minimal
changes to existing code. Show me the diff/new files before wiring into the
main handler so I can review the interface first.
