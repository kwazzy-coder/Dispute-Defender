"""Tests for vision attachments, the demo-password guard, webhook raw-body verification, SSRF guard,
the prediction track record, and the MCP server."""

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import mcp_server
from app import agent, main
from app.mock_paypal import MockPayPal
from app.paypal import PayPalClient
from app.store import store
from app.tools import ToolContext, execute_tool


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        c.post("/api/reset")
        yield c


# ---- vision attachments ------------------------------------------------------------------------


def test_view_attachment_returns_image_and_blocks_traversal(client):
    ctx = ToolContext(MockPayPal(), [])
    result, summary = asyncio.run(execute_tool("view_attachment", {"ref": "bowls-crack-1.png"}, ctx))
    assert result["image"]["media_type"] == "image/png" and len(result["image"]["data"]) > 1000
    assert result["url"] == "/api/attachments/bowls-crack-1.png"
    for bad in ("../seed.json", "seed.json", "..\\seed.json", "nope.png"):
        with pytest.raises(ValueError):
            asyncio.run(execute_tool("view_attachment", {"ref": bad}, ctx))
    # PayPal-hosted documents are only fetched from PayPal hosts.
    with pytest.raises(ValueError):
        asyncio.run(execute_tool("view_attachment", {"ref": "https://evil.example/x.png"}, ctx))


def test_attachment_endpoint_and_drawer_photos(client):
    r = client.get("/api/attachments/mugs-colour.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/api/attachments/..%2Fseed.json").status_code == 404
    photos = client.get("/api/disputes/PP-D-27488").json()["photos"]
    assert [p["name"] for p in photos] == ["bowls-crack-1.png", "bowls-crack-2.png", "box-corner.png"]


def test_agent_sends_image_block_to_claude(monkeypatch):
    final = {
        "dispute_type": "t", "summary": "s", "recommendation": "ACCEPT", "win_probability": 10, "reasoning": "r",
        "key_facts": [], "evidence": [], "tracking_carrier": "", "tracking_number": "", "refund_id": "",
        "offer_type": "NONE", "offer_amount": 0, "response_note": "n", "buyer_message": "b", "risks": [],
        "missing_evidence": [], "process_fix": "",
    }
    script = [
        SimpleNamespace(stop_reason="tool_use", model="m", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                        content=[SimpleNamespace(type="tool_use", id="v1", name="view_attachment", input={"ref": "box-corner.png"})]),
        SimpleNamespace(stop_reason="end_turn", model="m", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                        content=[SimpleNamespace(type="text", text=json.dumps(final))]),
    ]
    calls = []

    async def fake_create(client, **params):
        calls.append(params)
        return script[len(calls) - 1]

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(agent, "_client", lambda: None)
    store.load()
    result = asyncio.run(agent.investigate("PP-D-27488", ToolContext(MockPayPal(), []), lambda s: None))
    tool_result = calls[1]["messages"][2]["content"][0]
    assert [b["type"] for b in tool_result["content"]] == ["text", "image"]
    assert tool_result["content"][1]["source"]["media_type"] == "image/png"
    assert result["trace"][0]["attachment"] == "/api/attachments/box-corner.png"
    assert "cache_read_tokens" in result["engine"]


# ---- demo password -----------------------------------------------------------------------------


def test_demo_password_guards_actions_but_not_browsing(client, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, demo_password="s3cret"))
    assert client.get("/api/disputes").status_code == 200
    assert client.post("/api/disputes/PP-D-27544/analyze").status_code == 202  # analysis stays open
    assert client.post("/api/disputes/PP-D-27544/accept", json={"note": "x"}).status_code == 401
    assert client.post("/api/disputes/PP-D-27544/accept", json={"note": "x"}, headers={"X-Demo-Password": "wrong"}).status_code == 401
    assert client.post("/api/auth/check", headers={"X-Demo-Password": "s3cret"}).status_code == 200
    assert client.get("/api/config").json()["auth_required"] is True


# ---- PayPal: webhook raw-body verification & SSRF ----------------------------------------------


def test_webhook_verification_uses_raw_body_verbatim(monkeypatch):
    from app import paypal as mod

    monkeypatch.setattr(mod, "settings", dataclasses.replace(mod.settings, paypal_webhook_id="WH-1"))
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        sent["body"] = request.content
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    c = PayPalClient()
    c._http = httpx.AsyncClient(base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(handler))
    raw = b'{"id":"WH-EVT","event_type":"CUSTOMER.DISPUTE.CREATED","resource":{"amount":1.10,"note":"caf\\u00e9"}}'
    headers = {"paypal-auth-algo": "SHA256withRSA", "paypal-transmission-id": "t1"}
    assert asyncio.run(c.verify_webhook(headers, raw)) is True
    assert raw in sent["body"]  # not re-serialised: 1.10 and é survive byte-for-byte
    body = json.loads(sent["body"])
    assert body["webhook_id"] == "WH-1" and body["webhook_event"]["id"] == "WH-EVT"


def test_fetch_document_rejects_non_paypal_hosts():
    c = PayPalClient()
    for url in ("http://www.paypal.com/x", "https://paypal.com.evil.io/x", "https://169.254.169.254/latest"):
        with pytest.raises(ValueError):
            asyncio.run(c.fetch_document(url))


# ---- track record ------------------------------------------------------------------------------


def test_track_record_scores_predictions_against_rulings(client):
    client.post("/api/reset")
    for did in ("PP-D-27461", "PP-D-27515"):
        client.post(f"/api/disputes/{did}/analyze")
    import time

    deadline = time.time() + 20
    while time.time() < deadline and not all(client.get(f"/api/disputes/{d}").json()["analysis"] for d in ("PP-D-27461", "PP-D-27515")):
        time.sleep(0.3)
    client.post("/api/disputes/PP-D-27461/simulate-outcome", json={"outcome": "SELLER_FAVOR"})  # predicted 88% -> won
    client.post("/api/disputes/PP-D-27515/simulate-outcome", json={"outcome": "BUYER_FAVOR"})   # predicted 93% -> lost
    k = client.get("/api/disputes").json()["kpis"]
    assert k["track"]["graded"] == 2 and k["track"]["correct"] == 1
    assert k["track"]["brier"] == round(((0.88 - 1) ** 2 + (0.93 - 0) ** 2) / 2, 3)
    assert set(k["at_risk_by_verdict"]) == {"FIGHT", "OFFER", "ACCEPT", "NONE"}


# ---- MCP server --------------------------------------------------------------------------------


def test_mcp_server_tools_drive_the_app(client, monkeypatch):
    client.post("/api/reset")

    def bridge(request: httpx.Request) -> httpx.Response:
        r = client.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    monkeypatch.setattr(mcp_server, "_client", httpx.AsyncClient(base_url="http://dd", transport=httpx.MockTransport(bridge)))

    async def scenario():
        tools = {t.name: t for t in await mcp_server.server.list_tools()}
        assert {"list_disputes", "get_dispute", "investigate_dispute", "submit_evidence", "make_offer", "accept_claim", "dispute_patterns"} <= set(tools)
        assert tools["accept_claim"].annotations.destructive_hint is True
        assert tools["list_disputes"].annotations.read_only_hint is True
        listed = await mcp_server.list_disputes("needs_response")
        assert len(listed["disputes"]) == 6 and listed["disputes"][0]["dispute_id"] == "PP-D-27502"
        verdict = await mcp_server.investigate_dispute("PP-D-27461", wait_seconds=20)
        assert verdict["analysis"]["recommendation"] == "FIGHT"
        await mcp_server.submit_evidence("PP-D-27461")
        result = await mcp_server.server.call_tool("get_dispute", {"dispute_id": "PP-D-27461"})
        assert not result.is_error

    asyncio.run(scenario())
    assert client.get("/api/disputes/PP-D-27461").json()["dispute"]["status"] == "UNDER_REVIEW"
