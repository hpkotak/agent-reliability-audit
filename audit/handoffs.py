"""How the post-chat hand-off check (shop/followup.py) would have done on saved conversations.

    uv run python -m audit.handoffs results/claude-code results/claude-code-heldout

A missed hand-off is a conversation where the scenario needs a person and the agent never called
escalate. The check runs after the chat, so it can't change what the agent said; it shows how many
of those customers the application would have followed up with anyway, and how many other chats it
would have sent to a person.
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
            with tempfile.TemporaryDirectory() as tmp:
                con = rebuild(row, str(Path(tmp) / "shop.db"))
                flags = missed_handoffs(con)
                con.close()
            t = tally[f"{Path(folder).name} {row['model']}/{row['version']}"]
            t["chats"] += 1
            needed = scenarios[row["scenario"]]["expect"].get("escalated") is True and not row["escalated"]
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
