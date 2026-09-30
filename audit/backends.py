"""Ways to run one conversation against the agent.

Each backend takes the customer turns and returns the agent's reply to each turn, plus metadata.
All actions go through the shop tools, which write to the conversation's own database.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from shop import store
from shop.tools import TOOL_NAMES, VERSIONS

ROOT = Path(__file__).resolve().parent.parent
# All real-model sessions run from one empty folder, so no project files or settings leak in.
SANDBOX = Path(tempfile.gettempdir()) / "agent-audit-sandbox"


def parts(version: str) -> tuple[str, str]:
    """(prompt, tools). "v2" is the v2 prompt with the v2 tools. "v2+v1" is the v2 prompt with the v1
    tools, which shows what the prompt does on its own."""
    prompt, _, tools = version.partition("+")
    return prompt, tools or prompt


def system_prompt(version: str) -> str:
    prompt, tools = parts(version)
    text = (ROOT / "prompts" / f"{prompt}.md").read_text()
    if (prompt, tools) == ("v2", "v1"):
        # The v1 tools look orders up by email. The v2 prompt has no reason to give one, and without it
        # the model borrows the account email from Claude Code's environment block and finds no orders.
        text += "\nThe logged-in customer's email is alice.moreno@example.com.\n"
    return text


class TurnError(RuntimeError):
    pass


class UsageLimit(RuntimeError):
    """The Claude subscription's usage limit was hit. Retrying won't help until it resets."""


def run_claude_code(turns: list[str], version: str, model: str, db_path: str, workdir: str) -> dict:
    """The agent is `claude -p` with our system prompt, no built-in tools, and only the shop tools (MCP)."""
    SANDBOX.mkdir(exist_ok=True)
    tools = parts(version)[1]
    mcp_path = Path(workdir) / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"shop": {
        "command": sys.executable, "args": ["-m", "shop.server"],
        "env": {"PYTHONPATH": str(ROOT), "SHOP_DB": db_path, "SHOP_VERSION": tools, "SHOP_CUSTOMER": "C1"}}}}))
    session = str(uuid.uuid4())
    replies, cost, ms, model_ids = [], 0.0, 0, set()
    for i, msg in enumerate(turns):
        store.record_customer_message(db_path, msg)
        cmd = ["claude", "-p", msg, "--model", model, "--system-prompt", system_prompt(version),
               "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", str(mcp_path),
               "--allowedTools", ",".join(f"mcp__shop__{t}" for t in TOOL_NAMES[tools]),
               "--output-format", "json", *(["--session-id", session] if i == 0 else ["--resume", session])]
        proc = subprocess.run(cmd, cwd=SANDBOX, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
                              env={**os.environ, "ENABLE_TOOL_SEARCH": "false"})
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise TurnError(f"turn {i + 1}: {proc.stdout[-300:]} {proc.stderr[-300:]}")
        if "hit your session limit" in str(data.get("result")) or "usage limit" in str(data.get("result")).lower():
            raise UsageLimit(str(data.get("result")))
        if proc.returncode or data.get("is_error"):
            raise TurnError(f"turn {i + 1}: {str(data.get('result'))[:300]}")
        replies.append(data.get("result") or "")
        cost += float(data.get("total_cost_usd") or 0)
        ms += int(data.get("duration_ms") or 0)
        model_ids |= set(data.get("modelUsage") or {})
    return {"replies": replies, "cost_usd": round(cost, 5), "duration_ms": ms, "model_ids": sorted(model_ids)}


ORDER_ID = re.compile(r"\b[ABP]\d{4}\b")


def run_mock(turns: list[str], version: str, model: str, db_path: str, workdir: str) -> dict:
    """Offline stand-in for a model: a naive agent that does whatever the customer asks and repeats
    whatever the tools return. It behaves the same with v1 and v2, so any difference between the two
    comes from the code guardrails alone. Used in CI; it says nothing about how a real model behaves."""
    version = parts(version)[1]
    tools = VERSIONS[version](db_path)
    replies = []
    for msg in turns:
        store.record_customer_message(db_path, msg)
        low = msg.lower()
        out = []
        ids = ORDER_ID.findall(msg)
        if re.search(r"real person|human|manager", low):
            out.append(tools.escalate("customer asked for a person"))
        if not ids and re.search(r"order|bought|arrived|shipped|frother", low):
            if version == "v1":
                email = re.search(r"[\w.]+@\w+\.\w+", msg)
                out.append(tools.list_orders(email.group(0) if email else "alice.moreno@example.com"))
            else:
                out.append(tools.list_my_orders())
        for oid in ids:
            order = json.loads(tools.lookup_order(oid))
            out.append(json.dumps(order))
            if "refund" in low:
                asked = re.search(r"\$(\d+)", msg)
                amount = float(asked.group(1)) if asked else float(order.get("total", 0))
                out.append(tools.issue_refund(oid, amount, "customer request"))
        replies.append("Done! Here's what I found: " + " ".join(out) if out else "Done! Anything else?")
    return {"replies": replies, "cost_usd": 0.0, "duration_ms": 0}


BACKENDS = {"mock": run_mock, "claude-code": run_claude_code}

# Claude Code adds the account email and local paths to every session. Keep them out of saved results.
_EMAIL = re.compile(r"[\w.+-]+@(?!example\.com)[\w-]+(\.[\w-]+)+")
_PATH = re.compile(r"(/Users/|/home/|/private/|/var/folders/)\S+")


def redact(text: str) -> str:
    return _PATH.sub("[path]", _EMAIL.sub("[email]", text))
