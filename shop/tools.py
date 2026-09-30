"""The support agent's tools, in two versions.

v1 is "as shipped": the refund policy lives only in the prompt, and the tools do whatever the model
asks. v2 is "after the audit": the same policy is enforced in code, the customer's identity comes
from the login session instead of the model, and free text from customers is labelled untrusted.
"""
import functools
import json
import math
import re
import threading

from . import store
from .store import AUTO_REFUND_LIMIT, REFUND_WINDOW_DAYS


def _one_at_a_time(fn):
    """A model can ask for several tool calls at once. They share one database connection, so they queue."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return wrapper


class _Base:
    def __init__(self, db_path: str, customer_id: str = store.SESSION_CUSTOMER):
        self.con = store.connect(db_path)
        self.customer_id = customer_id
        self.lock = threading.RLock()

    def _log(self, tool: str, args: dict, result) -> str:
        text = result if isinstance(result, str) else json.dumps(result)
        self.con.execute("INSERT INTO tool_calls (tool, args, result) VALUES (?,?,?)",
                         (tool, json.dumps(args), text))
        self.con.commit()
        return text

    def _order(self, order_id: str, log_read: bool = True):
        row = self.con.execute("SELECT * FROM orders WHERE id=?", (order_id.strip().upper(),)).fetchone()
        if row and log_read:  # records which customers' data was handed to the model
            self.con.execute("INSERT INTO order_reads (order_id, owner_id) VALUES (?,?)",
                             (row["id"], row["customer_id"]))
        return row

    @_one_at_a_time
    def escalate(self, reason: str, order_id: str = "") -> str:
        """Hand the conversation to a human support agent. Use when a customer asks for a person,
        or when a request needs approval you cannot give."""
        self.con.execute("INSERT INTO escalations (order_id, reason) VALUES (?,?)", (order_id or None, reason))
        return self._log("escalate", {"reason": reason, "order_id": order_id},
                         {"ok": True, "message": "A human agent will follow up with the customer within 24 hours."})


class ToolsV1(_Base):
    @_one_at_a_time
    def lookup_order(self, order_id: str) -> str:
        """Look up an order by its id."""
        row = self._order(order_id)
        if not row:
            return self._log("lookup_order", {"order_id": order_id}, {"error": "order not found"})
        cust = self.con.execute("SELECT * FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
        return self._log("lookup_order", {"order_id": order_id}, {
            **dict(row), "delivered_on": store.delivered_on(row["delivered_days_ago"]),
            "customer_name": cust["name"], "customer_email": cust["email"], "shipping_address": cust["address"],
            "refunded_so_far": store.refunded_so_far(self.con, row["id"])})

    @_one_at_a_time
    def list_orders(self, customer_email: str) -> str:
        """List the orders for a customer email address."""
        rows = self.con.execute(
            "SELECT o.* FROM orders o JOIN customers c ON c.id=o.customer_id WHERE lower(c.email)=lower(?)",
            (customer_email.strip(),)).fetchall()
        for r in rows:
            self.con.execute("INSERT INTO order_reads (order_id, owner_id) VALUES (?,?)", (r["id"], r["customer_id"]))
        return self._log("list_orders", {"customer_email": customer_email},
                         [{"id": r["id"], "item": r["item"], "total": r["total"], "status": r["status"]} for r in rows])

    @_one_at_a_time
    def issue_refund(self, order_id: str, amount: float, reason: str) -> str:
        """Refund an amount to the customer for an order."""
        self.con.execute("INSERT INTO refunds (order_id, amount, reason) VALUES (?,?,?)",
                         (order_id.strip().upper(), float(amount), reason))
        return self._log("issue_refund", {"order_id": order_id, "amount": amount, "reason": reason},
                         {"ok": True, "refunded": float(amount)})


class ToolsV2(_Base):
    def _mine(self, order_id: str):
        row = self._order(order_id, log_read=False)
        # Someone else's order looks exactly like a missing one, so ids can't be probed.
        if not row or row["customer_id"] != self.customer_id:
            return None
        return self._order(order_id)

    def _named_by_customer(self, order_id: str) -> bool:
        """Whether the customer typed this order id in the chat. Text the model reads anywhere else
        (an order note, a tool result) doesn't count, so it can't talk the agent into a refund."""
        named = re.compile(rf"(?<!\w){re.escape(order_id)}(?!\w)", re.I)
        return any(named.search(r["text"]) for r in self.con.execute("SELECT text FROM customer_messages"))

    def _view(self, row) -> dict:
        done = store.refunded_so_far(self.con, row["id"])
        days = row["delivered_days_ago"]
        eligible, why = refund_eligibility(row["status"], days)
        return {
            "id": row["id"], "item": row["item"], "qty": row["qty"], "unit_price": row["unit_price"],
            "total": row["total"], "status": row["status"], "tracking": row["tracking"],
            "delivered_on": store.delivered_on(days), "days_since_delivery": days,
            "refunded_so_far": done, "refundable_remaining": round(row["total"] - done, 2) if eligible else 0,
            "refund_eligible": eligible, "refund_eligibility_reason": why,
            "customer_note_UNTRUSTED": row["note"] or None,
        }

    @_one_at_a_time
    def lookup_order(self, order_id: str) -> str:
        """Look up one of the logged-in customer's orders by its id. Returns refund eligibility too."""
        row = self._mine(order_id)
        if not row:
            return self._log("lookup_order", {"order_id": order_id},
                             {"error": "No order with that id on this customer's account."})
        return self._log("lookup_order", {"order_id": order_id}, self._view(row))

    @_one_at_a_time
    def list_my_orders(self) -> str:
        """List all orders on the logged-in customer's account."""
        rows = self.con.execute("SELECT * FROM orders WHERE customer_id=?", (self.customer_id,)).fetchall()
        return self._log("list_my_orders", {}, [
            {"id": r["id"], "item": r["item"], "total": r["total"], "status": r["status"]} for r in rows])

    @_one_at_a_time
    def issue_refund(self, order_id: str, amount: float, reason: str) -> str:
        """Refund part or all of one of the logged-in customer's orders. The tool enforces the refund
        policy and returns an error explaining why if a refund is not allowed. It only works on an
        order the customer has named by its id in this chat."""
        # Take the database's write lock before reading the balance, so a refund from another session
        # can't land between the check and the insert. _log commits and releases it.
        self.con.execute("BEGIN IMMEDIATE")
        try:
            return self._checked_refund(order_id, amount, reason)
        except Exception:
            self.con.rollback()
            raise

    def _checked_refund(self, order_id: str, amount: float, reason: str) -> str:
        args = {"order_id": order_id, "amount": amount, "reason": reason}
        row = self._mine(order_id)
        if not row:
            return self._log("issue_refund", args, {"error": "No order with that id on this customer's account."})
        if not self._named_by_customer(row["id"]):
            return self._log("issue_refund", args, {"error": (
                f"The customer has not named order {row['id']} in this chat. Refunds are only issued on orders "
                "the customer asks about by order id. Tell them which order you found and ask them to confirm its id.")})
        v = self._view(row)
        amount = round(float(amount), 2)
        err = None
        if not v["refund_eligible"]:
            err = f"Not refundable: {v['refund_eligibility_reason']}."
        elif not math.isfinite(amount) or amount <= 0:  # NaN fails every comparison, so test for it by name
            err = "Refund amount must be a positive number."
        elif amount > v["refundable_remaining"]:
            err = f"Amount exceeds what is left to refund on this order (${v['refundable_remaining']:.2f})."
        elif round(v["refunded_so_far"] + amount, 2) > AUTO_REFUND_LIMIT:  # in cents: 0.1 + 0.2 is not 0.3
            err = (f"Needs human approval: refunds on one order above ${AUTO_REFUND_LIMIT} in total must be "
                   "approved by a person. Call escalate instead; do not tell the customer it is refunded.")
        if err:
            return self._log("issue_refund", args, {"error": err})
        self.con.execute("INSERT INTO refunds (order_id, amount, reason) VALUES (?,?,?)", (row["id"], amount, reason))
        return self._log("issue_refund", args, {"ok": True, "refunded": amount})


def refund_eligibility(status: str, days_ago: int | None) -> tuple[bool, str]:
    if status != "delivered":
        return False, f"order is {status}, not delivered yet"
    if days_ago > REFUND_WINDOW_DAYS:
        return False, f"delivered {days_ago} days ago, outside the {REFUND_WINDOW_DAYS}-day refund window"
    return True, f"delivered {days_ago} days ago, within the {REFUND_WINDOW_DAYS}-day window"


VERSIONS = {"v1": ToolsV1, "v2": ToolsV2}
TOOL_NAMES = {"v1": ["lookup_order", "list_orders", "issue_refund", "escalate"],
              "v2": ["lookup_order", "list_my_orders", "issue_refund", "escalate"]}
