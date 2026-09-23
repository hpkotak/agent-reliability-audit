"""Run the scenario suite: every scenario x version x model, repeated --trials times.

    uv run python -m audit.run                                   # offline mock agent, a few seconds
    uv run python -m audit.run --backend claude-code --models haiku,opus --trials 5

Results are appended to <out>/results.jsonl as each conversation finishes, so an interrupted run
continues where it stopped when started again with the same --out.
"""
import argparse
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
            calls = [{"tool": r["tool"], "args": json.loads(r["args"]), "result": redact(r["result"])}
                     for r in con.execute("SELECT * FROM tool_calls ORDER BY id")]
            con.close()
            return {**result, "replies": replies, "tool_calls": calls,
                    "cost_usd": out["cost_usd"], "duration_ms": out["duration_ms"], "model_ids": out.get("model_ids", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=BACKENDS)
    ap.add_argument("--models", default="haiku", help="comma-separated Claude model aliases (ignored by mock)")
    ap.add_argument("--versions", default="v1,v2")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    models = ["mock"] if a.backend == "mock" else a.models.split(",")
    scenarios = [s for s in SCENARIOS if not a.only or s["id"] in a.only.split(",")]
    out = Path(a.out or ROOT / "results" / a.backend)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "results.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if "error" not in r:
                done.add((r["model"], r["version"], r["scenario"], r["trial"]))

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
                   "title": s["title"], "trial": t, "turns": s["turns"], **result}
            with lock:
                f.write(json.dumps(row) + "\n")
                f.flush()
            status = "ERROR" if "error" in row else ("pass" if row["passed"] else "FAIL")
            print(f"[{n}/{len(jobs)}] {m} {v} {s['id']} #{t}: {status}", flush=True)

    report.write(out)


if __name__ == "__main__":
    main()
