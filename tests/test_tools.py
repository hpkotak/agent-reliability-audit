import json

import pytest

from shop import store
from shop.tools import ToolsV1, ToolsV2


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "shop.db")
    store.create(path)
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


def test_v2_cumulative_limit_blocks_split_refunds(db):
    t = ToolsV2(db)
    assert call(t.issue_refund, "A1008", 150, "first part")["ok"]
    assert "human approval" in call(t.issue_refund, "A1008", 100, "second part")["error"]


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
