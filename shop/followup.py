"""Runs when a chat ends, outside the model. Finds refund requests that needed a person and never got
one, so the chat application can open the hand-off itself instead of trusting the model to call
escalate."""
import json
import re

from . import store
from .store import AUTO_REFUND_LIMIT, REFUND_WINDOW_DAYS

_ORDER = re.compile(r"(?<!\w)[A-Z]\d{4}(?!\w)", re.I)
_DOLLARS = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
_WANTS = re.compile(r"refund|money back|\bback\b", re.I)


def asked(messages: list[str]) -> dict[str, float | None]:
    """Dollars the customer asked to have refunded, per order. Amounts in a message belong to the last
    order it names, or to the last order named before it, so "refund $150 of A1008" then "please
    refund the other $100" is $250 on A1008. An order she asks to refund with no amount is None,
    meaning everything left on it. Messages that don't ask for money back are ignored."""
    out, last = {}, None
    for msg in messages:
        ids = [i.upper() for i in _ORDER.findall(msg)]
        last = ids[-1] if ids else last
        if not _WANTS.search(msg):
            continue
        for i in ids:
            out.setdefault(i, None)
        dollars = [float(d) for d in _DOLLARS.findall(msg)]
        if dollars and last:
            out[last] = (out.get(last) or 0) + sum(dollars)
    return out


def missed_handoffs(con) -> list[str]:
    """Why this chat needs a person to follow up, or [] if it doesn't."""
    if con.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]:
        return []
    reasons = []
    for (args, result) in con.execute("SELECT args, result FROM tool_calls WHERE tool='issue_refund'"):
        if "Needs human approval" in result:
            reasons.append(f"the refund tool said {json.loads(args)['order_id']} needs a person's approval")
    messages = [r[0] for r in con.execute("SELECT text FROM customer_messages ORDER BY id")]
    for order_id, amount in asked(messages).items():
        o = con.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, store.SESSION_CUSTOMER)).fetchone()
        if not o or o["status"] != "delivered" or o["delivered_days_ago"] > REFUND_WINDOW_DAYS:
            continue  # the policy refuses it outright, so there's nothing for a person to approve
        before = con.execute("SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE order_id=? AND seeded=1",
                             (order_id,)).fetchone()[0]  # refunded before this chat started
        left = o["total"] - before
        wanted = left if amount is None else min(amount, left)  # $999 on a $45 order is $45
        if wanted > 0 and round(before + wanted, 2) > AUTO_REFUND_LIMIT:
            reasons.append(f"she asked for ${wanted:g} on {order_id}, which needs a person's approval")
        if round(store.refunded_so_far(con, order_id), 2) > AUTO_REFUND_LIMIT:
            reasons.append(f"refunds on {order_id} passed ${AUTO_REFUND_LIMIT} without a person's approval")
    return sorted(set(reasons))
