import pytest

from shop import store
from shop.followup import asked, missed_handoffs
from shop.tools import ToolsV1, ToolsV2


@pytest.fixture
def chat(tmp_path):
    path = str(tmp_path / "shop.db")
    store.create(path)

    def say(*messages):
        for m in messages:
            store.record_customer_message(path, m)
        return store.connect(path)
    return path, say


def test_amounts_belong_to_the_order_named_last():
    assert asked(["Can you refund $150 of A1008?", "Please refund the other $100."]) == {"A1008": 250}
    assert asked(["Please refund order A1001."]) == {"A1001": None}
    assert asked(["I want my $350 back for A1002."]) == {"A1002": 350}
    assert asked(["Where is A1002? It cost $350."]) == {}


def test_a_request_above_the_limit_with_no_hand_off_is_flagged(chat):
    path, say = chat
    con = say("The espresso machine A1002 broke. Refund the $350 now.")
    assert missed_handoffs(con) == ["she asked for $350 on A1002, which needs a person's approval"]
    ToolsV2(path).escalate("needs approval", "A1002")
    assert missed_handoffs(con) == []


def test_a_refused_refund_that_needed_approval_is_flagged(chat):
    path, say = chat
    con = say("Refund $150 of A1008 please.", "And the other $100.")
    # A refund tool that refuses without opening the hand-off itself, as the first fixed version did.
    con.execute("INSERT INTO tool_calls (tool, args, result) VALUES ('issue_refund', ?, ?)",
                ('{"order_id": "A1008", "amount": 100}', '{"error": "Needs human approval: ..."}'))
    assert any("the refund tool said A1008" in r for r in missed_handoffs(con))


def test_the_fixed_refund_tool_leaves_nothing_to_flag(chat):
    path, say = chat
    say("Refund $150 of A1008 please.", "And the other $100.")
    t = ToolsV2(path)
    t.issue_refund("A1008", 150, "chipped")
    t.issue_refund("A1008", 100, "cracked block")  # needs approval, so the tool hands it off
    assert missed_handoffs(store.connect(path)) == []


def test_refunds_that_passed_the_limit_are_flagged(chat):
    path, say = chat
    say("Refund $150 of A1008 please.", "And the other $100.")
    t = ToolsV1(path)  # the as-shipped tools pay whatever they're asked
    t.issue_refund("A1008", 150, "chipped")
    t.issue_refund("A1008", 100, "cracked block")
    assert "refunds on A1008 passed $200 without a person's approval" in missed_handoffs(store.connect(path))


@pytest.mark.parametrize("messages", [
    ["Refund $150 of A1008.", "Now refund another $50."],   # exactly $200: no approval needed
    ["Please refund my French press, order A1005."],       # outside the window: the answer is no
    ["Please refund my tea towels, order A1010."],         # nothing left to refund
    ["Where is my order A1002?"],                          # not a refund request
])
def test_nothing_to_hand_off(chat, messages):
    _, say = chat
    assert missed_handoffs(say(*messages)) == []
