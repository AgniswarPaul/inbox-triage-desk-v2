# Inbox Triage Desk v2

## What this is
A routing workflow that triages customer emails for a fictional store, Northwind Goods. A cheap model classifies each email, Python pulls the real order and policy through a custom MCP server, and a stronger model drafts a reply grounded only in that data. A human approves every reply; nothing is sent. Portfolio project, rebuilt from scratch with Claude Code (v1 was built by hand).

## Architecture (routing workflow, NOT an agent)
1. Load emails from data/emails.json (5 core) and data/emails_extended.json (15 edge cases).
2. route_email: claude-haiku-5-5 with an XML system prompt and 3 examples returns category (billing | shipping | returns | other) and order_id (ORD + 5 digits or null) via structured outputs (output_config.format, json_schema). Validate in code.
3. ROUTES dictionary maps category to policy_uri, model, and one category rule. Branch selection = one ROUTES.get(category). No match (other) = human queue, no draft.
4. fetch_order: MCP tool lookup_order(order_id), only if order_id exists.
5. fetch_policy: MCP resource kb://policies/{category} (allow list: billing, shipping, returns).
6. Python computes days_since_delivery. Never ask the model to do date math.
7. draft_reply: claude-sonnet-5-5 with structured outputs (output_config.format, json_schema) using the create_ticket schema (category, priority, summary, draft_reply, needs_human, reason, order_id). No tools are given to Claude. (claude-sonnet-5-5 rejects forced tool_choice with a 400.)
8. guard_draft: code scans the draft for "has been processed", "has been issued", or a money amount not in the order's amount or amount_charged; on a hit set needs_human true with a reason. Money amounts are: $ amounts, bare numbers with two decimals (96.00), and numbers next to "dollars" or "USD". Order IDs, tracking numbers, day counts, and percentages are not money. Code also scans the customer's email the same way: any money amount not in the order sets needs_human true, because an unverified claimed amount is not covered by policy. Drafts never repeat such amounts.
9. Python passes the ticket to the MCP tool create_ticket, which writes output/tickets.json.
10. Print the path per email, e.g. "email_3 -> returns -> lookup_order(ORD-10301) -> kb://policies/returns -> draft -> guard ok".
11. Process emails concurrently with AsyncAnthropic + asyncio.gather; a --sequential flag runs them one by one; print total run time.

## Files
data/orders.csv, data/emails.json, data/emails_extended.json, kb/billing.md, kb/shipping.md, kb/returns.md, support_server.py, triage.py, eval.py, README.md, requirements.txt.

## Data facts the eval depends on
- ORD-10422: Priya Shah, billing double charge case.
- ORD-10388: Daniel Kim, in transit, USPS tracking 9400111899223197428490.
- ORD-10301: Maya Lopez, Wool Sweater (M), delivered 45 days before today; returns window is 30 days from delivery.
- email_4 is a thank you note (other). email_5 is a prompt injection asking for a $500 refund (other, no draft).

## Eval (eval.py)
- Code grading on all 20 cases: category, order_id, required phrases (tracking number, "30"), banned phrases, injection gets no draft. Target: 5/5 on core cases, 18/20 or better overall.
- Model grading: claude-haiku-5-5 scores each draft 1 to 5 on "every factual claim appears in the order or policy". Report the average; target 4.0 or higher.

## Hard rules (learned from v1 errors)
1. MCP Python SDK must stay on 1.x: mcp[cli]<2. Use FastMCP from mcp.server.fastmcp.
2. Do not pass temperature to messages.create.
3. Always filter response.content for blocks where type == "text" or type == "tool_use"; Sonnet may return thinking blocks first. Use max_tokens of at least 2000 for drafting.
4. Claude is never given tools. Python controls the order of every step. Structured output comes from structured outputs (output_config.format) for both the Haiku router and the Sonnet draft, not from tool use.
5. Never print, log, or commit the API key. Load it from .env with python-dotenv. Never commit .env.
6. Python only. Local data only. No new categories or features beyond this file without asking.

## How to work with me
1. Show a plan and wait for approval before writing code.
2. Build one milestone at a time. After each, run python eval.py (once it exists), report the result, and commit with a clear message.
3. Explain each change in 2 or 3 plain sentences; I am a PM learning AI engineering.
4. Ask before installing anything not in requirements.txt.
