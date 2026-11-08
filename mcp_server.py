"""Dispute Defender as an MCP server, so other agents (Claude Desktop, Claude Code, any MCP client)
can triage and act on PayPal disputes in conversation:

    "What disputes are due this week?"  ->  list_disputes
    "Investigate Daniel's chargeback"   ->  investigate_dispute
    "Fight it" / "Offer her $20"        ->  submit_evidence / make_offer   (client asks the user first)

It is a thin client over the running app's HTTP API (DD_URL, default http://localhost:8000), so the
dashboard, autopilot and MCP all share one source of truth.

Claude Desktop config (claude_desktop_config.json):
    {"mcpServers": {"dispute-defender": {
        "command": "/path/to/.venv/bin/python",
        "args": ["/path/to/mcp_server.py"],
        "env": {"DD_URL": "http://localhost:8000", "DEMO_PASSWORD": ""}}}}
"""

import asyncio
import os
import time

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

server = MCPServer(
    name="dispute-defender",
    instructions=(
        "Tools for a small merchant's PayPal disputes. Use list_disputes to triage, investigate_dispute to run the "
        "AI investigation, then confirm with the user before submit_evidence, make_offer or accept_claim - those "
        "act on the merchant's real PayPal account."
    ),
)

_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        headers = {"X-Demo-Password": os.environ["DEMO_PASSWORD"]} if os.getenv("DEMO_PASSWORD") else {}
        _client = httpx.AsyncClient(base_url=os.getenv("DD_URL", "http://localhost:8000"), headers=headers, timeout=60)
    return _client


async def _call(method: str, path: str, **kwargs) -> dict:
    resp = await http().request(method, path, **kwargs)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 400:
        raise RuntimeError(data.get("error") or data.get("detail") or f"HTTP {resp.status_code}")
    return data


def _brief(row: dict) -> dict:
    return {k: row.get(k) for k in (
        "dispute_id", "buyer", "item", "reason_label", "amount", "currency", "status", "days_left",
        "recommendation", "win_probability", "expected_recovery",
    )}


@server.tool(annotations=READ)
async def list_disputes(status: str = "open") -> dict:
    """List PayPal disputes, most urgent first, with the AI verdict where available.

    Args:
        status: "open" (default), "needs_response", or "all".
    """
    data = await _call("GET", "/api/disputes")
    rows = data["rows"]
    if status == "open":
        rows = [r for r in rows if r["is_open"]]
    elif status == "needs_response":
        rows = [r for r in rows if r["needs_response"]]
    return {"kpis": data["kpis"], "disputes": [_brief(r) for r in rows]}


@server.tool(annotations=READ)
async def get_dispute(dispute_id: str) -> dict:
    """Full detail for one dispute: buyer claim, AI verdict, key facts, evidence, drafted response."""
    data = await _call("GET", f"/api/disputes/{dispute_id}")
    a = data.get("analysis") or {}
    return {
        **_brief(data["row"]),
        "buyer_messages": [m.get("content") for m in data["dispute"].get("messages", [])],
        "analysis": {k: a.get(k) for k in (
            "dispute_type", "summary", "recommendation", "win_probability", "reasoning", "key_facts", "evidence",
            "offer_type", "offer_amount", "response_note", "buyer_message", "risks", "missing_evidence", "process_fix",
        )} if a else None,
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
async def investigate_dispute(dispute_id: str, wait_seconds: int = 120) -> dict:
    """Run (or re-run) the AI investigation on a dispute and wait for the verdict."""
    before = (await _call("GET", f"/api/disputes/{dispute_id}")).get("analysis") or {}
    await _call("POST", f"/api/disputes/{dispute_id}/analyze")
    deadline = time.monotonic() + max(5, min(wait_seconds, 300))
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        data = await _call("GET", f"/api/disputes/{dispute_id}")
        a = data.get("analysis")
        if a and not data["row"]["analyzing"] and a.get("analyzed_at") != before.get("analyzed_at"):
            return await get_dispute(dispute_id)
    return {"dispute_id": dispute_id, "status": "still running - call get_dispute shortly"}


@server.tool(annotations=ACT)
async def submit_evidence(dispute_id: str, note: str = "") -> dict:
    """Submit the agent's evidence package to PayPal. Uses the drafted statement unless `note` is given.
    Irreversible - confirm with the user first."""
    if not note:
        a = (await _call("GET", f"/api/disputes/{dispute_id}")).get("analysis") or {}
        note = a.get("response_note") or ""
    if not note:
        raise RuntimeError("Investigate the dispute first, or pass a note.")
    return await _call("POST", f"/api/disputes/{dispute_id}/evidence", json={"note": note})


@server.tool(annotations=ACT)
async def make_offer(dispute_id: str, offer_type: str, amount: float, note: str = "") -> dict:
    """Offer the buyer a resolution. offer_type: REFUND, REFUND_WITH_RETURN, REFUND_WITH_REPLACEMENT or
    REPLACEMENT_WITHOUT_REFUND. Confirm the amount with the user first."""
    note = note or "We'd like to resolve this directly with the offer below."
    return await _call("POST", f"/api/disputes/{dispute_id}/offer", json={"note": note, "offer_type": offer_type, "amount": amount})


@server.tool(annotations=ACT)
async def accept_claim(dispute_id: str, note: str = "") -> dict:
    """Accept the claim: PayPal refunds the buyer the full disputed amount. Confirm with the user first."""
    note = note or "We accept this claim and are refunding the buyer. Sorry for the trouble."
    return await _call("POST", f"/api/disputes/{dispute_id}/accept", json={"note": note})


@server.tool(annotations=READ)
async def dispute_patterns() -> dict:
    """Root-cause analysis across all disputes, with concrete fixes."""
    return await _call("POST", "/api/insights")


if __name__ == "__main__":
    server.run("stdio")
