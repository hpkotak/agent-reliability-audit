"""How the post-chat hand-off check (shop/followup.py) would have done on saved conversations.

    uv run python -m audit.handoffs results/claude-code results/claude-code-heldout

A missed hand-off is a conversation where the scenario needs a person and the agent made no hand-off
(the fixed refund tool's automatic one counts). The check runs after the chat, so it can't change what
the agent said. The fixed version runs it on every chat; this shows how many missed hand-offs it
catches, and how many other chats it sends to a person, in every setup.
"""
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from audit.regrade import rebuild
from audit.run import HELD_OUT, SCENARIOS
from shop.followup import missed_handoffs


def main(folders: list[str]):
    scenarios = {s["id"]: s for s in SCENARIOS + HELD_OUT}
    tally = defaultdict(lambda: {"missed": 0, "caught": 0, "extra": 0, "chats": 0})
    examples = []
    for folder in folders:
        for line in (Path(folder) / "results.jsonl").read_text().splitlines():
            row = json.loads(line)
            if "error" in row:
                continue
            # Judge the agent alone: leave out any hand-off the check already opened after this chat.
            agent_only = {**row, "tool_calls": [c for c in row["tool_calls"] if c["tool"] != "post_chat_handoff"]}
            with tempfile.TemporaryDirectory() as tmp:
                con = rebuild(agent_only, str(Path(tmp) / "shop.db"))
                agent_escalated = con.execute("SELECT COUNT(*) FROM escalations").fetchone()[0] > 0
                flags = missed_handoffs(con)
                con.close()
            t = tally[f"{Path(folder).name} {row['model']}/{row['version']}"]
            t["chats"] += 1
            needed = scenarios[row["scenario"]]["expect"].get("escalated") is True and not agent_escalated
            t["missed"] += needed
            t["caught"] += needed and bool(flags)
            t["extra"] += bool(flags) and not needed
            if flags and not needed:
                examples.append(f"{Path(folder).name} {row['model']} {row['version']} {row['scenario']} #{row['trial']}: {flags}")
    print("| Results | Setup | Chats | Hand-offs the agent missed | Caught by the check | Other chats sent to a person |")
    print("| --- | --- | --- | --- | --- | --- |")
    for k, t in tally.items():
        folder, setup = k.split(" ")
        print(f"| {folder} | {setup} | {t['chats']} | {t['missed']} | {t['caught']} | {t['extra']} |")
    print("\nOther chats sent to a person:")
    print("\n".join(f"- {e}" for e in examples) or "- none")


if __name__ == "__main__":
    main(sys.argv[1:])
