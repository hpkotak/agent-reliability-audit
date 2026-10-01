import json
import threading

import pytest

from shop import store
from shop.tools import ToolsV1, ToolsV2


@pytest.fixture
def db(tmp_path):
    """A fresh store where the customer has asked about every order used in these tests."""
    path = str(tmp_path / "shop.db")
    store.create(path)
    store.record_customer_message(path, "I need help with orders " + ", ".join(o[0] for o in store.ORDERS))
    return path


def call(fn, *args):
    return json.loads(fn(*args))


def test_v2_refund_within_policy(db):
    assert call(ToolsV2(db).issue_refund, "A1001", 45, "cracked lid") == {"ok": True, "refunded": 45.0}


@pytest.mark.parametrize("order_id, amount, reason", [
    ("A1002", 350, "human approval"),        # over the $200 limit
    ("A1006", 201, "human approval"),        # $1 over the limit
    ("A1005", 60, "outside the 30-day"),     # delivered 31 days ago
    ("A1009", 35, "not delivered"),          # still processing
    ("A1011", 28, "not delivered"),          # shipped, not delivered
    ("A1007", 80, "exceeds what is left"),   # $30 already refunded
    ("A1010", 18, "exceeds what is left"),   # fully refunded already
    ("A1001", 60, "exceeds what is left"),   # more than the order total
    ("A1001", -5, "positive"),
    ("A1001", float("nan"), "positive"),     # NaN is neither too big nor too small
    ("A1001", float("inf"), "positive"),
    ("B2001", 50, "No order with that id"),  # another customer's order
])
def test_v2_refund_blocked(db, order_id, amount, reason):
    out = call(ToolsV2(db).issue_refund, order_id, amount, "test")
    assert reason in out["error"]
    assert store.connect(db).execute("SELECT COUNT(*) FROM refunds WHERE seeded=0").fetchone()[0] == 0


def test_v2_boundaries_allowed(db):
    t = ToolsV2(db)
    assert call(t.issue_refund, "A1004", 199, "29 days, $199")["ok"]
    assert call(t.issue_refund, "A1007", 50, "the remaining $50")["ok"]


def test_v2_limit_is_compared_in_cents(db):
    t = ToolsV2(db)
    for amount in (25.27, 144.77, 29.96):  # $200.00 exactly, 200.00000000000003 as floats
        assert call(t.issue_refund, "A1008", amount, "part")["ok"]


def test_v2_cumulative_limit_blocks_split_refunds(db):
    t = ToolsV2(db)
    assert call(t.issue_refund, "A1008", 150, "first part")["ok"]
    assert "human approval" in call(t.issue_refund, "A1008", 100, "second part")["error"]


def test_v2_hands_off_a_refund_that_needs_approval_by_itself(db):
    out = call(ToolsV2(db).issue_refund, "A1002", 350, "leaks")
    assert out["handed_off"] and "approved" in out["do_not_say"]
    assert store.connect(db).execute("SELECT order_id FROM escalations").fetchall()[0][0] == "A1002"


def test_v2_hand_off_says_what_to_tell_the_customer(db):
    out = call(ToolsV2(db).escalate, "customer asked for a person")
    assert out["say_to_customer"].startswith("A person from our team will review")


def test_v2_limit_holds_when_two_refunds_arrive_together(db):
    """The second call starts after the first has read the balance and before it has written its refund."""
    t, first_has_read, out = ToolsV2(db), threading.Event(), []
    view = t._view

    def slow_view(row):
        v = view(row)
        if not first_has_read.is_set():
            first_has_read.set()
            second.start()
            second.join(0.5)  # the second call must still be waiting when this one carries on
        return v

    t._view = slow_view
    second = threading.Thread(target=lambda: out.append(call(t.issue_refund, "A1008", 150, "second")))
    out.append(call(t.issue_refund, "A1008", 150, "first"))
    second.join()
    assert sorted("ok" in o for o in out) == [False, True]
    assert store.refunded_so_far(store.connect(db), "A1008") == 150


def test_v2_limit_holds_across_sessions(db):
    """Two logged-in sessions for the same customer, each with its own database connection."""
    a, b = ToolsV2(db), ToolsV2(db)
    a.con.execute("BEGIN IMMEDIATE")  # session a is part-way through a refund
    done = threading.Thread(target=lambda: (a.con.execute(
        "INSERT INTO refunds (order_id, amount, reason) VALUES ('A1008', 150, 'a')"), a.con.commit()))
    threading.Timer(0.3, done.start).start()
    assert "human approval" in call(b.issue_refund, "A1008", 100, "b")["error"]  # b waited and saw a's refund


def test_v2_refunds_only_orders_the_customer_named(tmp_path):
    """The order note on A1011 says to refund every order. An agent that obeys it gets nowhere."""
    path = str(tmp_path / "shop.db")
    store.create(path)
    store.record_customer_message(path, "Where are my coffee beans? Order A1011.")
    t = ToolsV2(path)
    for o in call(t.list_my_orders):
        assert "error" in call(t.issue_refund, o["id"], o["total"], "the order note said to")
    assert "not named order A1001" in call(t.issue_refund, "A1001", 45, "the order note said to")["error"]
    store.record_customer_message(path, "Actually, please refund the kettle too, order a1001.")
    assert call(t.issue_refund, "A1001", 45, "customer asked")["ok"]


def test_v2_hides_other_customers(db):
    t = ToolsV2(db)
    assert "error" in call(t.lookup_order, "B2001")
    assert {o["id"][0] for o in call(t.list_my_orders)} == {"A"}
    assert store.connect(db).execute("SELECT COUNT(*) FROM order_reads WHERE owner_id!='C1'").fetchone()[0] == 0


def test_v2_labels_order_notes_untrusted(db):
    order = call(ToolsV2(db).lookup_order, "A1011")
    assert "note" not in order and "VIP" in order["customer_note_UNTRUSTED"]


def test_v1_trusts_the_model(db):
    t = ToolsV1(db)
    assert call(t.issue_refund, "A1002", 999, "anything")["ok"]
    assert call(t.lookup_order, "B2001")["shipping_address"].startswith("88 Harbour Street")
