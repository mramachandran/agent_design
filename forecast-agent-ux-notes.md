# Forecast Agent — UX Gap Notes

Four features to close the gap between the current app and a Gemini-style experience. Backend: Gemini + BigQuery (forecast table + actuals table). Frontend: custom JavaScript.

---

## 1. Streaming stage narration (replacing the static "thinking")

Instead of surfacing Gemini's reasoning tokens, narrate your own pipeline via **server-sent events (SSE)**.

**Mechanism**
- The answer endpoint becomes a streaming response; the handler is a generator that `yield`s.
- Before each stage, emit a small event: `{"type": "status", "label": "Reading forecast table"}`.
- When the answer is ready, emit `{"type": "answer", ...}` (and optionally stream the answer text itself in chunks).
- Frontend uses `EventSource` (or `fetch` + ReadableStream) and swaps the label in the thinking element as each status arrives.

**Stage list for this flow**
1. Understanding the question
2. Resolving context (pin down zip, date range, metric)
3. Querying the forecast table
4. Querying actuals
5. Comparing forecast vs. actuals
6. Building the chart
7. Writing the summary

Skip whichever stages don't apply to a given question.

**Sketch (Python / Flask-style)**

```python
def answer_stream(question, history, state):
    yield sse({"type": "status", "label": "Understanding the question"})
    intent = parse_intent(question, history, state)

    yield sse({"type": "status", "label": "Resolving context"})
    ctx = resolve_context(intent, state)   # zip, dates, metric

    yield sse({"type": "status", "label": "Reading forecast table"})
    forecast = query_forecast(ctx)

    yield sse({"type": "status", "label": "Reading actuals"})
    actuals = query_actuals(ctx)

    yield sse({"type": "status", "label": "Comparing forecast vs. actuals"})
    comparison = compare(forecast, actuals)

    yield sse({"type": "status", "label": "Building the chart"})
    chart = build_chart_spec(comparison)

    yield sse({"type": "status", "label": "Writing the summary"})
    text = llm_summarize(question, comparison)

    yield sse({"type": "answer", "text": text, "chart": chart})

def sse(obj):
    return f"data: {json.dumps(obj)}\n\n"
```

```javascript
// Frontend
const es = new EventSource(`/ask?...`);
es.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === "status") thinkingEl.textContent = msg.label;
  if (msg.type === "answer") { renderAnswer(msg); es.close(); }
};
```

Note: Gemini *does* expose model thinking via the API (thinking budget + thought summaries, streamed as parts flagged as thoughts), but self-narration gets ~90% of the feel at zero token cost.

---

## 2. Conversation history + context resolution ("this zip", two questions back)

There's no special controller — the LLM resolves references by reading recent turns you send it. What you control is **how much history goes in**, and that's the design decision.

**Pattern: sliding window + structured state**
- Send the last **6–10 turns** to the model each time.
- Separately maintain a small **state object** in your backend/session:

```json
{
  "last_zip": "15224",
  "last_date_range": ["2026-09-07", "2026-09-13"],
  "last_metric": "package_volume",
  "last_result_shape": "table"
}
```

- Update it after every turn; inject it into the prompt alongside the history.
- The state survives even when the original turn falls out of the window — much more reliable than hoping the model scrolls back far enough.
- "Draw a table for this zip" two questions later resolves from `last_zip` even if the window missed it.

---

## 3. Unknown terms → clarify, not "I don't know"

**a) Column glossary in the system prompt.** Plain-English descriptions of every field:

```
online_channel: shipments originating from web/app orders (what users may call "e-commerce" or "online shopping")
residential_flag: delivery to a residential address
...
```

Half the unknowns disappear here — the model maps user wording ("e-commerce") to real columns.

**b) Explicit clarify action.** Give the model two actions instead of one:

- `run_query(sql)` — normal path
- `clarify(question, options)` — when a term can't be mapped

The clarify response offers real schema-derived options: *"I don't have an e-commerce field — did you mean the `online_channel` column, or shipments with `residential_flag`?"* A failure becomes a conversational turn, not an error message.

---

## 4. Suggested follow-up chips

Don't let the model invent follow-ups freely — generate **2–3 suggestions grounded in your actual schema** after each answer.

- Derive them from what just happened + what the schema supports:
  - "Break that down by region"
  - "Show the same week last year"
  - "Compare against the forecast for next week"
- Render as clickable chips under the answer; clicking sends the text as the next user turn (which also keeps your history/state loop working with no special handling).
- Cheap implementation: one extra small LLM call with the schema summary + last Q&A, asking for exactly 3 short follow-ups as JSON.

---

## Suggested order of implementation

1. SSE stage narration (biggest perceived-quality win, no model changes)
2. State object + sliding window (fixes "this zip" reliability)
3. Glossary + clarify action (fixes dead ends)
4. Follow-up chips (polish; reuses the same plumbing)
