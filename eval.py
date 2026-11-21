"""Eval for the Northwind Goods triage workflow.

Three parts:
  1. Guard unit checks: deterministic, no API calls.
  2. Code grading on all 20 emails: category, order_id, draft or no draft, needs_human,
     required phrases, banned phrases. Each email passes only if every check passes.
  3. Model grading: claude-haiku-5-5 scores each draft 1-5 on
     "every factual claim appears in the order or policy".

Targets: 5/5 core, 18/20 or better overall, average grounding 4.0 or higher.

Usage:
  python eval.py                    # today pinned to 2026-11-20 so results are reproducible
  python eval.py --sequential
"""

import argparse
import asyncio
import json
import sys
from datetime import date

import anthropic

from triage import BASE_DIR, Usage, first_text, guard_draft, load_emails, run_pipeline

EVAL_TODAY = date(2026, 11, 20)
JUDGE_MODEL = "claude-haiku-5-5"
CORE_IDS = {f"email_{i}" for i in range(1, 6)}

# ---------------------------------------------------------------- 1. guard unit checks

GUARD_ORDER = {"found": True, "amount": "48.00", "amount_charged": "96.00"}
GUARD_CASES = [
    ("The amount charged (96.00) is higher than the order amount (48.00).", False),
    ("We will refund the duplicate $48 once verified.", False),
    ("Refunds appear within 5 to 7 business days.", False),
    ("Order ORD-10422 was delivered 45 days ago.", False),
    ("Tracking: 9400111899223197428490 and 7712 3456 7890.", False),
    ("Enjoy 10% off, or 12.50% on your next order.", False),
    ("We will refund 95.99 to your card.", True),
    ("You will receive $150 back.", True),
    ("A fee of 12.50 USD was added.", True),
    ("That is 15 dollars.", True),
    ("Your refund has been issued.", True),
    ("Your return has been processed.", True),
]


def run_guard_checks() -> tuple[int, int]:
    passed = 0
    print("Guard unit checks")
    for text, should_flag in GUARD_CASES:
        flagged = guard_draft(text, GUARD_ORDER) is not None
        ok = flagged == should_flag
        passed += ok
        if not ok:
            print(f"  FAIL  expected flag={should_flag}: {text!r}")
    print(f"  {passed}/{len(GUARD_CASES)} passed\n")
    return passed, len(GUARD_CASES)


# ---------------------------------------------------------------- 2. code grading


def grade_case(email: dict, result: dict) -> list[str]:
    """Return a list of failure reasons; empty means PASS."""
    exp = email["expected"]
    if result.get("error"):
        return [f"pipeline error: {result['error']}"]
    fails = []
    ticket = result["ticket"]
    draft = ticket.get("draft_reply") or ""

    if result["category"] != exp["category"]:
        fails.append(f"category {result['category']!r} != expected {exp['category']!r}")
    if result["order_id"] != exp["order_id"]:
        fails.append(f"order_id {result['order_id']!r} != expected {exp['order_id']!r}")
    has_draft = bool(ticket.get("draft_reply"))
    if has_draft != exp["has_draft"]:
        fails.append(f"has_draft {has_draft} != expected {exp['has_draft']}")
    if "needs_human" in exp and ticket["needs_human"] != exp["needs_human"]:
        fails.append(f"needs_human {ticket['needs_human']} != expected {exp['needs_human']} "
                     f"(reason: {ticket['reason']})")
    for phrase in exp.get("required_phrases", []):
        if phrase.lower() not in draft.lower():
            fails.append(f"missing required phrase {phrase!r}")
    for phrase in exp.get("banned_phrases", []):
        if phrase.lower() in draft.lower():
            fails.append(f"contains banned phrase {phrase!r}")
    return fails


# ---------------------------------------------------------------- 3. model grading

JUDGE_SYSTEM = """You grade customer support drafts for factual grounding.
Criterion: every factual claim in the draft appears in the order data, the policy, or the computed facts.
Factual claims include order details, amounts, dates, day counts, statuses, carriers, tracking numbers,
timelines, and promises about what the store will do. Greetings, apologies, and sign-offs are not claims.
The customer's email is shown for context only; repeating what the customer said is not a grounded fact.

Score 1 to 5:
5 = every claim is supported
4 = one minor claim is loosely supported or slightly overstated
3 = one claim is unsupported
2 = several claims are unsupported
1 = the draft is mostly invented or contradicts the data"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "unsupported_claims"],
    "additionalProperties": False,
}


async def judge_draft(client, email: dict, result: dict, usage: Usage) -> dict:
    order = result.get("order")
    user = (
        f"<customer_email>{email['body']}</customer_email>\n"
        f"<order>{json.dumps(order) if order else 'No order data.'}</order>\n"
        f"<policy>{result.get('policy', '')}</policy>\n"
        f"<facts>{json.dumps(result.get('facts', {}))}</facts>\n"
        f"<draft>{result['ticket']['draft_reply']}</draft>"
    )
    response = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=2000,
        system=JUDGE_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    usage.add(JUDGE_MODEL, response.usage)
    return json.loads(first_text(response))


# ---------------------------------------------------------------- main


async def main_async(sequential: bool) -> int:
    guard_passed, guard_total = run_guard_checks()

    emails = load_emails()
    by_id = {e["id"]: e for e in emails}
    print(f"Running pipeline on {len(emails)} emails (today={EVAL_TODAY.isoformat()})\n")
    results, pipeline_usage, elapsed = await run_pipeline(emails, EVAL_TODAY, sequential, verbose=False)

    judge_usage = Usage()
    client = anthropic.AsyncAnthropic()  # .env already loaded by run_pipeline
    drafted = [r for r in results if r.get("ticket") and r["ticket"].get("draft_reply")]
    judgments = await asyncio.gather(
        *(judge_draft(client, by_id[r["email_id"]], r, judge_usage) for r in drafted))
    scores = {r["email_id"]: j for r, j in zip(drafted, judgments)}

    print(f"{'case':<9} {'result':<6} {'grounding':<9} path")
    core_pass = overall_pass = 0
    failures = []
    for r in results:
        eid = r["email_id"]
        fails = grade_case(by_id[eid], r)
        ok = not fails
        overall_pass += ok
        core_pass += ok and eid in CORE_IDS
        g = scores.get(eid)
        print(f"{eid:<9} {'PASS' if ok else 'FAIL':<6} {str(g['score']) if g else '-':<9} {r['path']}")
        if fails:
            failures.append((eid, by_id[eid].get("edge_case", "core case"), fails))

    avg = sum(j["score"] for j in judgments) / len(judgments) if judgments else 0.0
    core_total = sum(1 for e in emails if e["id"] in CORE_IDS)

    if failures:
        print("\nFailures")
        for eid, label, fails in failures:
            print(f"  {eid} ({label})")
            for f in fails:
                print(f"    - {f}")

    low = [(eid, j) for eid, j in scores.items() if j["score"] < 5]
    if low:
        print("\nGrounding scores below 5")
        for eid, j in low:
            print(f"  {eid}: {j['score']} - {'; '.join(j['unsupported_claims']) or 'no claims listed'}")

    print("\nSummary")
    print(f"  Guard unit checks: {guard_passed}/{guard_total}")
    print(f"  Core:    {core_pass}/{core_total}   (target {core_total}/{core_total})")
    print(f"  Overall: {overall_pass}/{len(results)}  (target 18/20)")
    print(f"  Avg grounding (Haiku judge, {len(judgments)} drafts): {avg:.2f}  (target 4.0)")
    print(f"  Pipeline run time: {elapsed:.2f}s ({'sequential' if sequential else 'concurrent'})")

    print("\nTokens")
    for label, u in (("pipeline", pipeline_usage), ("judge", judge_usage)):
        for model, m in u.by_model.items():
            print(f"  {label:<8} {model}: {m['input']} input / {m['output']} output")
    p_in, p_out = pipeline_usage.totals()
    j_in, j_out = judge_usage.totals()
    print(f"  Total: {p_in + j_in} input / {p_out + j_out} output")

    met = (core_pass == core_total and overall_pass >= 18 and avg >= 4.0
           and guard_passed == guard_total)
    print(f"\nTargets {'MET' if met else 'MISSED'}")
    return 0 if met else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the triage workflow.")
    parser.add_argument("--sequential", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.sequential)))


if __name__ == "__main__":
    main()
