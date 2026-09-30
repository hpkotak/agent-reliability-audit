"""Run the scenario suite: every scenario x version x model, repeated --trials times.

    uv run python -m audit.run                                   # offline mock agent, a few seconds
    uv run python -m audit.run --backend claude-code --models haiku,opus --trials 5

Results are appended to <out>/results.jsonl as each conversation finishes, so an interrupted run
continues where it stopped when started again with the same --out. Saved conversations are only
reused if they came from the same prompts, tools and scenarios; otherwise pass --fresh or a new --out.
"""
import argparse
import hashlib
import json
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from audit import report
from audit.backends import BACKENDS, ROOT, TurnError, UsageLimit, redact
from audit.grade import grade
from shop import store

SCENARIOS = yaml.safe_load((ROOT / "audit" / "scenarios.yaml").read_text())
# Written after the prompts and tools were final, and never used to change them.
HELD_OUT = yaml.safe_load((ROOT / "audit" / "heldout.yaml").read_text())
SUITES = {"main": SCENARIOS, "heldout": HELD_OUT}


def suite_id(scenarios: list[dict]) -> str:
    """Fingerprint of everything that decides how a conversation goes: prompts, tools, backends and the
    customer's messages. The grader and the expected outcomes are left out, because saved conversations
    can be graded again (audit.regrade)."""
    h = hashlib.sha256()
    for f in sorted([*(ROOT / "prompts").glob("*.md"), *(ROOT / "shop").glob("*.py"), ROOT / "audit" / "backends.py"]):
        h.update(f.read_bytes())
    h.update(json.dumps([[s["id"], s["turns"]] for s in scenarios]).encode())
    return h.hexdigest()[:12]


def run_one(backend: str, model: str, version: str, scn: dict, trial: int, attempts: int = 3) -> dict:
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "shop.db")
            store.create(db)
            try:
                out = BACKENDS[backend](scn["turns"], version, model, db, tmp)
            except (TurnError, subprocess.TimeoutExpired, OSError) as e:
                if attempt == attempts:
                    return {"error": redact(str(e))}
                time.sleep(30 * attempt)  # usually a rate limit; start the conversation again on a fresh database
                continue
            con = store.connect(db)
            crash = con.execute("SELECT error FROM harness_errors").fetchone()
            if crash:
                con.close()
                return {"error": "tool crashed: " + redact(crash[0][-500:])}
            replies = [redact(r) for r in out["replies"]]
            result = grade(scn, con, replies)
            calls = [{"tool": r["tool"], "args": json.loads(redact(r["args"])), "result": redact(r["result"])}
                     for r in con.execute("SELECT * FROM tool_calls ORDER BY id")]
            con.close()
            return {**result, "replies": replies, "tool_calls": calls,
                    "cost_usd": out["cost_usd"], "duration_ms": out["duration_ms"], "model_ids": out.get("model_ids", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=BACKENDS)
    ap.add_argument("--models", default="haiku", help="comma-separated Claude model aliases or full model ids (ignored by mock)")
    ap.add_argument("--versions", default="v1,v2", help='comma-separated; "v2+v1" is the v2 prompt with the v1 tools')
    ap.add_argument("--suite", default="main", choices=SUITES)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="")
    ap.add_argument("--fresh", action="store_true", help="discard conversations already saved in --out and run them all")
    a = ap.parse_args()

    models = ["mock"] if a.backend == "mock" else a.models.split(",")
    scenarios = [s for s in SUITES[a.suite] if not a.only or s["id"] in a.only.split(",")]
    out = Path(a.out or ROOT / "results" / (a.backend if a.suite == "main" else f"{a.backend}-{a.suite}"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / "results.jsonl"
    suite = suite_id(SUITES[a.suite])
    if a.fresh or a.backend == "mock":  # the mock takes seconds, so it always starts over
        path.unlink(missing_ok=True)
    done = set()
    if path.exists():
        saved = [json.loads(line) for line in path.read_text().splitlines()]
        stale = sum(r.get("suite") != suite for r in saved)
        if stale:
            raise SystemExit(f"{path} holds {stale} conversations from a different version of the prompts, tools or "
                             "scenarios, so this run can't continue from them. Pass --out <new folder> to keep them, "
                             "or --fresh to replace them.")
        done = {(r["model"], r["version"], r["scenario"], r["trial"]) for r in saved if "error" not in r}

    jobs = [(m, v, s, t) for m in models for v in a.versions.split(",") for s in scenarios
            for t in range(1, a.trials + 1) if (m, v, s["id"], t) not in done]
    print(f"{len(jobs)} conversations to run ({len(done)} already done) -> {path}")
    lock = threading.Lock()
    with ThreadPoolExecutor(a.workers) as pool, path.open("a") as f:
        futures = {pool.submit(run_one, a.backend, m, v, s, t): (m, v, s, t) for m, v, s, t in jobs}
        for n, fut in enumerate(as_completed(futures), 1):
            m, v, s, t = futures[fut]
            try:
                result = fut.result()
            except UsageLimit as e:
                print(f"Stopping: {e}. Run the same command again after it resets to continue.", flush=True)
                for other in futures:
                    other.cancel()
                break
            row = {"model": m, "version": v, "scenario": s["id"], "category": s["category"],
                   "title": s["title"], "trial": t, "turns": s["turns"], "suite": suite, **result}
            with lock:
                f.write(json.dumps(row) + "\n")
                f.flush()
            status = "ERROR" if "error" in row else ("pass" if row["passed"] else "FAIL")
            print(f"[{n}/{len(jobs)}] {m} {v} {s['id']} #{t}: {status}", flush=True)

    report.write(out)


if __name__ == "__main__":
    main()
