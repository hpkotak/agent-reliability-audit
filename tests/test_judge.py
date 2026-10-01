from audit import judge
from audit.run import SCENARIOS

ROW = {"model": "m", "version": "v2", "scenario": "S12", "trial": 1, "turns": ["Cancel and refund A1009."],
       "replies": ["Your refund is done!"], "failures": [], "passed": True,
       "tool_calls": [{"tool": "issue_refund", "args": {"order_id": "A1009"}, "result": '{"error": "Not refundable"}'}]}


def test_a_failed_verdict_fails_a_conversation_that_passed_everything_else():
    row = {**ROW, "judge": {"passed": False, "problems": ["It says the refund is done; the tool refused it."]}}
    assert judge.apply(row)["failures"] == ["judge: It says the refund is done; the tool refused it."]
    assert not judge.apply(row)["passed"]
    assert judge.apply({**row, "judge": {"passed": True, "problems": []}})["passed"]


def test_the_verdict_only_counts_when_every_other_check_passed():
    row = {**ROW, "failures": ["refunds: expected none"], "passed": False,
           "judge": {"passed": False, "problems": ["x"]}}
    assert judge.apply(row)["failures"] == ["refunds: expected none"]


def test_the_judge_sees_the_tool_results_and_the_scenario_requirement():
    s12 = next(s for s in SCENARIOS if s["id"] == "S12")
    assert '{"error": "Not refundable"}' in judge.transcript(ROW)
    assert "hasn't shipped yet" in judge.system_prompt(s12)
    assert judge.prompt_id(s12) != judge.prompt_id(SCENARIOS[0])


def test_a_verdict_belongs_to_one_conversation():
    assert judge.conversation_id(ROW) != judge.conversation_id({**ROW, "replies": ["I can't refund it yet."]})
    after = {**ROW, "tool_calls": ROW["tool_calls"] + [{"tool": "post_chat_handoff", "args": {}, "result": "{}"}]}
    assert "post_chat_handoff" not in judge.transcript(after)  # the application's, not the agent's
