"""Northwind Goods support MCP server.

Exposes local data to the triage pipeline over MCP (stdio):
  - tool     lookup_order(order_id)      -> order row from data/orders.csv
  - resource kb://policies/{category}    -> policy text from kb/{category}.md
  - tool     create_ticket(...)          -> appends a ticket to output/tickets.json

Python in triage.py decides when each of these is called; the model never does.
"""

import asyncio
import csv
import json
import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
ORDERS_CSV = BASE_DIR / "data" / "orders.csv"
KB_DIR = BASE_DIR / "kb"
# TICKETS_PATH lets tests write somewhere other than the real output file.
TICKETS_PATH = Path(os.environ.get("TICKETS_PATH", BASE_DIR / "output" / "tickets.json"))

ORDER_ID_RE = re.compile(r"^ORD-\d{5}$")
POLICY_CATEGORIES = {"billing", "shipping", "returns"}
TICKET_CATEGORIES = {"billing", "shipping", "returns", "other"}
PRIORITIES = {"low", "medium", "high"}

# WARNING level keeps per-request INFO lines out of the triage output.
mcp = FastMCP("northwind-support", log_level="WARNING")

# One lock so concurrent create_ticket calls never interleave their writes.
_tickets_lock = asyncio.Lock()


def _load_orders() -> dict[str, dict]:
    with ORDERS_CSV.open(newline="", encoding="utf-8") as f:
        return {row["order_id"]: row for row in csv.DictReader(f)}


@mcp.tool()
def lookup_order(order_id: str) -> dict:
    """Look up one order by ID (format ORD-12345). Returns found=false if missing."""
    if not isinstance(order_id, str) or not ORDER_ID_RE.match(order_id):
        return {"found": False, "error": "invalid order_id format", "order_id": order_id}
    order = _load_orders().get(order_id)
    if order is None:
        return {"found": False, "error": "order not found", "order_id": order_id}
    return {"found": True, **order}


@mcp.resource("kb://policies/{category}")
def get_policy(category: str) -> str:
    """Return the policy text for billing, shipping, or returns."""
    if category not in POLICY_CATEGORIES:
        raise ValueError(f"unknown policy category: {category!r}")
    return (KB_DIR / f"{category}.md").read_text(encoding="utf-8")


@mcp.tool()
async def create_ticket(
    email_id: str,
    category: str,
    priority: str,
    summary: str,
    draft_reply: str | None,
    needs_human: bool,
    reason: str,
    order_id: str | None = None,
) -> dict:
    """Save a triage ticket to output/tickets.json for human review. Nothing is sent."""
    if category not in TICKET_CATEGORIES:
        raise ValueError(f"invalid category: {category!r}")
    if priority not in PRIORITIES:
        raise ValueError(f"invalid priority: {priority!r}")
    if order_id is not None and not ORDER_ID_RE.match(order_id):
        raise ValueError(f"invalid order_id: {order_id!r}")

    ticket = {
        "email_id": email_id,
        "category": category,
        "priority": priority,
        "summary": summary,
        "draft_reply": draft_reply,
        "needs_human": bool(needs_human),
        "reason": reason,
        "order_id": order_id,
    }

    async with _tickets_lock:
        TICKETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tickets = []
        if TICKETS_PATH.exists():
            tickets = json.loads(TICKETS_PATH.read_text(encoding="utf-8") or "[]")
        tickets.append(ticket)
        tmp = TICKETS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(tickets, indent=2), encoding="utf-8")
        os.replace(tmp, TICKETS_PATH)  # atomic swap, never a half-written file

    return {"saved": True, "path": str(TICKETS_PATH), "ticket_count": len(tickets)}


if __name__ == "__main__":
    mcp.run()  # stdio transport
