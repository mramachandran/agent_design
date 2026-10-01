# Feature Prompt: Stage Instrumentation + Trace View for an NL-to-SQL Agent

## Context (paste this as-is so the assistant understands the system)

I have a Flask backend (`main.py`) that turns a user's natural-language
question into a BigQuery query, runs it, and returns a narrated answer.

Pipeline today:
- **Router:** Gemini (flash model) reads the question and picks an intent /
  extracts parameters (`USE_GEMINI_ROUTER=true`), with a template/keyword
  path as the alternative.
- **SQL:** parameterized templates filled from the router output, with an
  LLM-generated SQL fallback.
- **Execution:** BigQuery, always under the app's service identity.
- **Narration:** Gemini writes the final answer from the returned rows.
- An offline judge already exists in `eval_answers.py`.

**The problem:** end-to-end latency is 50–80 seconds per question, and I
can't tell which stage is responsible. I also can't see what the model
*understood* from the question before SQL was built.

**What I want:** instrument every stage so each request produces a trace,
then build a simple local UI to inspect traces. Do NOT change routing,
SQL generation, or execution behaviour. This is observability only.

---

## Part 1: Stage instrumentation

### 1.1 Stages

Every request must emit exactly one record per stage it passes through:

| seq | stage        | input                         | output                                     | model? |
|-----|--------------|-------------------------------|--------------------------------------------|--------|
| 1   | `extract`    | raw question (+ history)      | router decision / parameter object         | yes    |
| 2   | `resolve`    | raw entity strings            | resolved IDs, or an `ambiguities` list     | no     |
| 3   | `build_sql`  | resolved params               | SQL string, template name, dry-run bytes   | maybe  |
| 4   | `execute`    | SQL                           | row count, bytes processed, slot ms, job id | no    |
| 5   | `synthesize` | question + rows               | natural-language answer                    | yes    |

If a stage doesn't exist yet in the code (e.g. `resolve`), emit it anyway
as a no-op with `duration_ms` ≈ 0 so traces are always five rows. If the
pipeline makes **more** model calls than shown here (retries, a second
routing pass, reasoning calls), give each its own record with a distinct
stage name — hidden extra model round trips are the most likely cause of
the latency and must be visible.

### 1.2 Record schema

```json
{
  "trace_id": "uuid4, one per request",
  "session_id": "string or null",
  "question": "string (repeated on every record for easy filtering)",
  "stage": "extract | resolve | build_sql | execute | synthesize | <extra>",
  "seq": 1,
  "input": {},
  "output": {},
  "started_at": "ISO-8601 UTC",
  "duration_ms": 0,
  "model": "model id or null",
  "thinking_budget_or_effort": "value or null",
  "tokens_in": 0,
  "tokens_out": 0,
  "thinking_tokens": 0,
  "cache_hit": false,
  "error": null
}
```

Notes:
- `output` for `extract` must be the **full structured object the model
  returned** (intent, parameters, any ambiguity flags). This is "what the
  model understood" — the most important field for debugging.
- For model stages, read token counts from the response's usage metadata,
  including thinking tokens where the SDK reports them.
- For `execute`, capture `job.total_bytes_processed`, `job.slot_millis`,
  and split timing into **query time** (job created → job done) and
  **download time** (job done → rows in a DataFrame/list). These two
  numbers tell us whether BigQuery or the network is slow.
- Set `cache_hit: true` when `RESPONSE_CACHE` short-circuits the request.
- Truncate large payloads: rows to the first 20, strings to 4,000 chars.

### 1.3 Implementation

- Add a small module `tracing.py` with:
  - `new_trace_id()`
  - a context manager `stage(trace_id, name, seq, **meta)` that yields a
    mutable record, times the block with `time.perf_counter()`, captures
    exceptions into `error` (then re-raises), and appends the record on exit.
  - a pluggable sink: default writes JSONL to `traces/YYYY-MM-DD.jsonl`;
    keep it behind a `TraceSink` interface so it can later go to Cloud
    Logging or BigQuery without touching call sites.
- Gate everything behind `TRACING_ENABLED` (default `true` locally,
  `false` in prod until reviewed). When disabled, the context manager is a
  no-op with no measurable overhead.
- Return `trace_id` in the API response so the frontend/CLI can link to it.
- Add a `X-Trace-Id` response header as well.
- Never log credentials, access tokens, or full user OAuth payloads.

### 1.4 CLI test runner

Add `trace_run.py`:

```
python3 trace_run.py "What's the total forecast for Pittsburgh?"
python3 trace_run.py --file questions.txt
```

For each question, run it through Flask's test client and print a compact
table: stage, duration_ms, model, tokens, and a one-line summary of the
output. End with a total and the slowest stage highlighted.

---

## Part 2: Trace view UI (Streamlit, single file `trace_view.py`)

Keep it simple — a debug tool, not a dashboard.

**Sidebar**
- Date picker (which JSONL file), text filter on question, toggle
  "errors only", toggle "slower than N seconds".

**Main — trace list**
- One row per `trace_id`: time, question, total ms, number of model calls,
  error badge.

**Main — selected trace**
- Stacked horizontal bar of stage durations (one segment per stage).
- One expander per stage showing `duration_ms`, model, tokens, then the
  raw `input` and `output` JSON.
- The `extract` expander opens by default — it shows what the model
  understood.

**Main — aggregate tab**
- p50 / p90 duration per stage across all traces in the selected file.
- One stacked bar per trace (last 50), so the dominant stage is obvious
  at a glance.

Run with `streamlit run trace_view.py`. No auth, local only.

---

## Part 3: Hook into evaluation

- Make `eval_answers.py` (and any routing eval script) attach the
  `trace_id` for every question to its report, so a failing eval case
  links straight to its trace.
- Add a `--tag` arg that writes into each trace record (e.g.
  `--tag baseline-flash-2.5`, `--tag flash-3.5-low`) so runs can be
  compared side by side in the UI's aggregate tab.
- Extraction accuracy stays deterministic (field-level exact match against
  expected parameters). The LLM judge is used only for the final answer,
  offline, never in the live request path.

---

## Acceptance criteria

1. Running one question produces exactly one `trace_id` with ≥5 stage
   records in the JSONL file.
2. The sum of stage `duration_ms` is within 5% of measured end-to-end time
   (if not, there is an uninstrumented gap — find and instrument it).
3. `execute` shows separate query and download timings.
4. Every model call in the request path appears as its own record.
5. `trace_view.py` loads a day's file and shows the stacked timing bar.
6. With `TRACING_ENABLED=false`, behaviour and responses are unchanged.
7. Existing tests and eval scripts still pass.

## Order of work

1. `tracing.py` + wrap the five stages in `main.py`
2. `trace_run.py`, run 10 representative questions, report where time goes
3. `trace_view.py`
4. Eval hook (`trace_id` + `--tag`)
5. Only then: swap the router model / lower thinking effort as a tagged
   run and compare against baseline

Before writing code, list the exact functions in `main.py` you will wrap
for each stage and confirm with me.
