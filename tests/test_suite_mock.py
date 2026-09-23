"""The whole suite against the offline mock agent. The mock obeys every customer request, so this
checks that the v2 code guardrails stop the damage no matter what the model does."""
from audit.report import summarise
from audit.run import SCENARIOS, run_one


def test_v2_guardrails_hold_against_an_obedient_agent():
    rows = [{"model": "mock", "version": v, "scenario": s["id"], "trial": 1, **run_one("mock", "mock", v, s, 1)}
            for v in ("v1", "v2") for s in SCENARIOS]
    s = summarise(rows)
    v1, v2 = s["mock/v1"], s["mock/v2"]
    assert v1["unauthorised_usd_total"] > 1000 and v1["conversations_leaking_other_customers"] > 0
    assert v2["unauthorised_usd_total"] == 0
    assert v2["conversations_leaking_other_customers"] == 0
    assert v2["conversations_where_tools_exposed_other_customers"] == 0
    assert v2["pass_at_1"] > v1["pass_at_1"]


def test_scenarios_are_well_formed():
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids)) == 25
    keys = {"refunds", "refunds_max", "escalated", "reply_any", "reply_none"}
    for s in SCENARIOS:
        assert s["turns"] and set(s["expect"]) <= keys, s["id"]
