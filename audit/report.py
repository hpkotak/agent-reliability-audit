"""Turns results.jsonl into summary.json and REPORT.md."""
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def load(out: Path) -> list[dict]:
    rows = {}
    for line in (out / "results.jsonl").read_text().splitlines():
        r = json.loads(line)
        key = (r["model"], r["version"], r["scenario"], r["trial"])
        if "error" not in r or key not in rows:  # a successful retry replaces an earlier error
            rows[key] = r
    return list(rows.values())


def summarise(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["version"])].append(r)
    summary = {}
    for (model, version), rs in sorted(groups.items()):
        ok = [r for r in rs if "error" not in r]
        by_scn = defaultdict(list)
        for r in ok:
            by_scn[r["scenario"]].append(r["passed"])
        costs = [r["cost_usd"] for r in ok]
        summary[f"{model}/{version}"] = {
            "model": model, "version": version, "conversations": len(ok), "errors": len(rs) - len(ok),
            "trials_per_scenario": max((len(v) for v in by_scn.values()), default=0),
            "pass_at_1": round(sum(r["passed"] for r in ok) / len(ok), 3) if ok else None,
            "pass_all_k": round(sum(all(v) for v in by_scn.values()) / len(by_scn), 3) if by_scn else None,
            "scenarios_always_pass": sum(all(v) for v in by_scn.values()),
            "scenarios_never_pass": sum(not any(v) for v in by_scn.values()),
            "scenarios": len(by_scn),
            "unauthorised_usd_total": round(sum(r["unauthorised_usd"] for r in ok), 2),
            "conversations_with_unauthorised_refunds": sum(r["unauthorised_usd"] > 0 for r in ok),
            "conversations_leaking_other_customers": sum(any("leaked" in f for f in r["failures"]) for r in ok),
            "conversations_where_tools_exposed_other_customers": sum(r["other_customer_reads"] > 0 for r in ok),
            "mean_cost_usd": round(sum(costs) / len(costs), 4) if costs else 0,
            "median_seconds": round(median(r["duration_ms"] for r in ok) / 1000, 1) if ok else 0,
        }
    return summary


def pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def write(out: Path) -> None:
    rows = load(out)
    summary = summarise(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    keys = list(summary)
    L = ["# Audit results", "",
         "Every scenario is run several times per setup. **pass@1** is the share of single conversations",
         "that passed. **pass^k** is the share of scenarios that passed in *every* one of their k runs,",
         "which is what a customer-facing agent actually needs.", "",
         "| Setup | Conversations | pass@1 | pass^k | Unauthorised refunds | Conversations leaking other customers' data | Tools exposed other customers' data | Mean cost | Median time |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, s in summary.items():
        L.append(f"| {k} | {s['conversations']} ({s['errors']} errors) | {pct(s['pass_at_1'])} | "
                 f"{pct(s['pass_all_k'])} (k={s['trials_per_scenario']}) | ${s['unauthorised_usd_total']:,.0f} in "
                 f"{s['conversations_with_unauthorised_refunds']} | {s['conversations_leaking_other_customers']} | "
                 f"{s['conversations_where_tools_exposed_other_customers']} | ${s['mean_cost_usd']:.3f} | {s['median_seconds']}s |")

    by = defaultdict(list)
    for r in rows:
        if "error" not in r:
            by[(r["scenario"], f"{r['model']}/{r['version']}")].append(r)
    scns = sorted({(r["scenario"], r["category"], r["title"]) for r in rows})
    L += ["", "## Per scenario (runs passed)", "", "| Scenario | Category | " + " | ".join(keys) + " |",
          "| --- | --- | " + " | ".join("---" for _ in keys) + " |"]
    for sid, cat, title in scns:
        cells = []
        for k in keys:
            rs = by.get((sid, k), [])
            n = sum(r["passed"] for r in rs)
            cells.append(f"{n}/{len(rs)}" + ("" if n == len(rs) else " ❌" if n == 0 else " ⚠️"))
        L.append(f"| {sid} {title} | {cat} | " + " | ".join(cells) + " |")

    L += ["", "## Failures", ""]
    for sid, cat, title in scns:
        for k in keys:
            fails = [r for r in by.get((sid, k), []) if not r["passed"]]
            if not fails:
                continue
            reasons = Counter(f for r in fails for f in r["failures"])
            L += [f"### {sid} {title} ({k}): failed {len(fails)}/{len(by[(sid, k)])}", ""]
            L += [f"- {reason} (x{n})" for reason, n in reasons.most_common()]
            ex = fails[0]
            L += ["", "<details><summary>Example conversation</summary>", ""]
            for i, turn in enumerate(ex["turns"]):
                L += [f"**Customer:** {turn}", ""]
                if i < len(ex["replies"]):
                    L += [f"**Agent:** {ex['replies'][i]}".replace("\n", "\n> "), ""]
            L += ["Tool calls:", "", "```"]
            L += [f"{c['tool']}({json.dumps(c['args'])}) -> {c['result'][:300]}" for c in ex["tool_calls"]]
            L += ["```", "", "</details>", ""]
    (out / "REPORT.md").write_text("\n".join(L) + "\n")
    print(f"wrote {out / 'REPORT.md'}")
    for k, s in summary.items():
        print(f"  {k}: pass@1 {pct(s['pass_at_1'])}, pass^k {pct(s['pass_all_k'])}, "
              f"unauthorised ${s['unauthorised_usd_total']:,.0f}, leaks {s['conversations_leaking_other_customers']}")
