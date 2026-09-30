"""Re-grade saved conversations with the current grader and scenario checks, without calling a model.

    uv run python -m audit.regrade results/claude-code

Each conversation's database is rebuilt from its saved tool calls, then graded again against the
customer messages saved with it. What the agent did (refunds, hand-offs, other customers' orders
read) must come out the same as when it ran; only the verdict on it can change.

The rebuild starts from today's store fixtures (shop/store.py), because conversations don't save
them. Change a fixture, such as a customer's name, and the leak check looks for the new value in old
replies. Re-run the conversations instead of re-grading them after a fixture change.
"""
import json
import sys
import tempfile
from pathlib import Path

from audit import judge, report
from audit.grade import grade
from audit.run import HELD_OUT, SCENARIOS
from shop import store

ACTIONS = ("refunds", "escalated", "unauthorised_usd", "other_customer_reads")


def rebuild(row: dict, path: str):
    """The parts of the conversation's database that the grader reads: refunds, hand-offs, tool calls,
    which other customers' orders the tools returned, and what the customer typed."""
    store.create(path)
    for turn in row["turns"]:
        store.record_customer_message(path, turn)
    con = store.connect(path)
    owner = {r["id"]: r["customer_id"] for r in con.execute("SELECT id, customer_id FROM orders")}
    for c in row["tool_calls"]:
        con.execute("INSERT INTO tool_calls (tool, args, result) VALUES (?,?,?)",
                    (c["tool"], json.dumps(c["args"]), c["result"]))
        out = json.loads(c["result"])
        if c["tool"] == "escalate":
            con.execute("INSERT INTO escalations (order_id, reason) VALUES (?,?)",
                        (c["args"].get("order_id") or None, c["args"]["reason"]))
        elif c["tool"] == "issue_refund" and out.get("ok"):
            con.execute("INSERT INTO refunds (order_id, amount, reason) VALUES (?,?,?)",
                        (c["args"]["order_id"].strip().upper(), out["refunded"], c["args"]["reason"]))
        elif c["tool"] in ("lookup_order", "list_orders"):
            for o in out if isinstance(out, list) else [out]:
                if o.get("id") in owner:
                    con.execute("INSERT INTO order_reads (order_id, owner_id) VALUES (?,?)", (o["id"], owner[o["id"]]))
    con.commit()
    return con


def main(folder: str):
    out = Path(folder)
    scenarios = {s["id"]: s for s in SCENARIOS + HELD_OUT}
    rows, changed = [], []
    for line in (out / "results.jsonl").read_text().splitlines():
        row = json.loads(line)
        if "error" not in row:
            with tempfile.TemporaryDirectory() as tmp:
                con = rebuild(row, str(Path(tmp) / "shop.db"))
                new = grade({**scenarios[row["scenario"]], "turns": row["turns"]}, con, row["replies"])
                con.close()
            for k in ACTIONS:
                if new[k] != row[k]:
                    raise SystemExit(f"{row['model']} {row['version']} {row['scenario']} #{row['trial']}: rebuilt "
                                     f"{k} is {new[k]!r}, but {row[k]!r} was recorded. Not re-grading.")
            new = judge.apply({**row, **new})  # keeps the judge's verdict, if it judged this conversation
            if new["failures"] != row["failures"]:
                changed.append((row, new))
            row = new
        rows.append(row)
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for row, new in changed:
        verdict = "" if row["passed"] == new["passed"] else f" ({'pass' if row['passed'] else 'fail'} -> {'pass' if new['passed'] else 'FAIL'})"
        print(f"{row['model']} {row['version']} {row['scenario']} #{row['trial']}{verdict}: {new['failures']}")
    print(f"re-graded {len(rows)} conversations, {len(changed)} with different findings")
    report.write(out)


if __name__ == "__main__":
    main(sys.argv[1])
