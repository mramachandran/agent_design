"""
eval_answers.py - end-to-end answer evaluation with an LLM judge.

Runs each eval question through the FULL pipeline (router -> SQL -> BigQuery
-> narration) via Flask's test client, then asks a judge model to grade the
final answer against the rows it was based on.

Usage (Cloud Shell, in backend/):
  USE_GEMINI_ROUTER=true python3 eval_answers.py --category yoy_trend
  USE_GEMINI_ROUTER=true python3 eval_answers.py --limit 20 --json answers_report.json

CHECK these names against main.py before running:
  - request fields:  question, history, session_id
  - response fields: answer, route/intent, sql, rows (or data)
  - RESPONSE_CACHE, client (google-genai), types
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from main import app, RESPONSE_CACHE, client, types  # noqa: adjust if names differ
from eval_routing_dataset import EVAL_CASES

JUDGE_MODEL = "gemini-2.5-pro"  # stronger than the router model on purpose
MAX_ROWS_TO_JUDGE = 50

JUDGE_PROMPT = """You are grading an answer from a holiday peak-season data agent.

Context:
- "This Peak" = 2026 FORECAST (Nov 27 - Dec 24, 2026).
- "Last Peak" = 2025 ACTUALS; "past Peaks" = 2025 + 2024 actuals.
- YoY compares matching Peak days (same weekday/holiday), not calendar dates.
- Out-of-scope topics (online orders, customers, gift intent, international)
  must be declined, not guessed.

QUESTION: {question}
ROUTE: {route}
SQL: {sql}
ROWS (the only facts the answer may use): {rows}
ANSWER: {answer}

Grade each criterion PASS or FAIL:
1. grounded      - every number in ANSWER appears in (or is directly computed from) ROWS
2. answers_question - addresses the metric, grain, filters and dates asked
3. time_frame    - forecast vs actuals and the year(s) are used correctly
4. honest        - no invented facts; says so when data is missing or out of scope

Return JSON only."""

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "grounded": {"type": "STRING", "enum": ["PASS", "FAIL"]},
        "answers_question": {"type": "STRING", "enum": ["PASS", "FAIL"]},
        "time_frame": {"type": "STRING", "enum": ["PASS", "FAIL"]},
        "honest": {"type": "STRING", "enum": ["PASS", "FAIL"]},
        "reason": {"type": "STRING"},
    },
    "required": ["grounded", "answers_question", "time_frame", "honest", "reason"],
}
CRITERIA = ["grounded", "answers_question", "time_frame", "honest"]


def ask(question, tag):
    """Run one question through the real /api/ask handler, uncached."""
    RESPONSE_CACHE.clear()
    with app.test_client() as c:
        r = c.post("/api/ask", json={"question": question, "history": [],
                                     "session_id": f"answer-eval-{tag}"})
    body = r.get_json() or {}
    return {
        "status": r.status_code,
        "answer": body.get("answer", ""),
        "route": body.get("route") or body.get("intent"),
        "sql": body.get("sql", ""),
        "rows": (body.get("rows") or body.get("data") or [])[:MAX_ROWS_TO_JUDGE],
    }


def judge(question, resp):
    prompt = JUDGE_PROMPT.format(
        question=question, route=resp["route"], sql=resp["sql"] or "(none)",
        rows=json.dumps(resp["rows"], default=str), answer=resp["answer"])
    out = client.models.generate_content(
        model=JUDGE_MODEL, contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0, response_mime_type="application/json",
            response_schema=JUDGE_SCHEMA))
    return json.loads(out.text)


def run_case(args):
    i, (question, expected, category, notes) = args
    resp = ask(question, i)
    result = {"question": question, "category": category,
              "expected_intents": sorted(expected), **resp}
    if resp["status"] != 200:
        result["verdict"] = {"reason": f"HTTP {resp['status']}"}
        result["passed"] = False
        return result
    try:
        v = judge(question, resp)
    except Exception as e:  # judge failure shouldn't kill the run
        v = {"reason": f"JUDGE ERROR: {e}"}
    result["verdict"] = v
    result["passed"] = all(v.get(k) == "PASS" for k in CRITERIA)
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--category")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=5)  # BigQuery + 2 Gemini calls each
    p.add_argument("--json", default="answers_report.json")
    a = p.parse_args()

    cases = [c for c in EVAL_CASES if not a.category or c[2] == a.category]
    if a.limit:
        cases = cases[: a.limit]

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(run_case, enumerate(cases)))

    for r in results:
        tag = "PASS" if r["passed"] else "FAIL"
        print(f"[{tag}] ({r['category']}) {r['question']!r} -> {r['route']}")
        if not r["passed"]:
            print(f"       {r['verdict'].get('reason', '')}")

    print("\n=== Criterion pass rates ===")
    judged = [r for r in results if "grounded" in r["verdict"]]
    for k in CRITERIA:
        n = sum(r["verdict"].get(k) == "PASS" for r in judged)
        print(f"  {k:18s} {n}/{len(judged)}")
    total = sum(r["passed"] for r in results)
    print(f"  {'ALL':18s} {total}/{len(results)}")

    json.dump(results, open(a.json, "w"), indent=2, default=str)
    print(f"\nWrote {a.json}")


if __name__ == "__main__":
    main()
