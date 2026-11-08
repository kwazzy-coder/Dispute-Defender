import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent
from app.evidence import build_evidences
from app.main import app
from app.store import store
from app.tools import ToolContext

EXPECTED = {
    "PP-D-27461": "FIGHT",   # delivered + signed, to the PayPal address
    "PP-D-27488": "ACCEPT",  # documented transit damage, poor packing
    "PP-D-27502": "FIGHT",   # repeat customer, same device, signed delivery
    "PP-D-27515": "FIGHT",   # refund already issued
    "PP-D-27530": "OFFER",   # handmade variation - partial refund for one mug
    "PP-D-27544": "FIGHT",   # two separate orders to two addresses
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.post("/api/reset")
        yield c


def wait_for_analysis(client, dispute_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/disputes/{dispute_id}").json()
        if data["analysis"] and not data["row"]["analyzing"]:
            return data
        time.sleep(0.3)
    raise AssertionError(f"analysis for {dispute_id} did not finish")


def test_config_and_listing(client):
    cfg = client.get("/api/config").json()
    assert cfg["mode"] == "mock" and cfg["ai_enabled"] is False
    data = client.get("/api/disputes").json()
    assert len(data["rows"]) == 8
    assert data["kpis"]["open"] == 6
    assert data["kpis"]["won"] == 1 and data["kpis"]["lost"] == 1
    # Most urgent open dispute sorts first.
    assert data["rows"][0]["dispute_id"] == "PP-D-27502"


def test_rule_engine_recommendations(client):
    queued = client.post("/api/analyze-all").json()["queued"]
    assert set(queued) == set(EXPECTED)
    for dispute_id, rec in EXPECTED.items():
        result = wait_for_analysis(client, dispute_id)["analysis"]
        assert result["recommendation"] == rec, (dispute_id, result["recommendation"])
        assert result["trace"] and all(step["ok"] for step in result["trace"])
    refund = client.get("/api/disputes/PP-D-27515").json()["analysis"]
    assert refund["refund_id"] == "4LS40972TB3318256"
    mugs = client.get("/api/disputes/PP-D-27530").json()["analysis"]
    assert mugs["offer_amount"] == 24.5


def test_evidence_payload_has_tracking_and_refund_ids():
    analysis = {
        "evidence": [
            {"evidence_type": "PROOF_OF_FULFILLMENT", "title": "Delivered", "details": "x", "source": "USPS"},
            {"evidence_type": "PROOF_OF_REFUND", "title": "Refund", "details": "y", "source": "PayPal"},
        ],
        "tracking_carrier": "USPS",
        "tracking_number": "9400111202555842761234",
        "refund_id": "4LS40972TB3318256",
    }
    ev = build_evidences(analysis, "statement", "evidence.pdf")
    assert ev[0]["evidence_info"]["tracking_info"][0] == {"tracking_number": "9400111202555842761234", "carrier_name": "USPS"}
    assert ev[0]["documents"] == [{"name": "evidence.pdf"}] and ev[0]["notes"] == "statement"
    assert ev[1]["evidence_info"]["refund_ids"] == ["4LS40972TB3318256"]


def test_submit_evidence_and_pdf(client):
    pdf = client.get("/api/disputes/PP-D-27461/evidence.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    r = client.post("/api/disputes/PP-D-27461/evidence", json={"note": "The vase was delivered and signed for."})
    assert r.status_code == 200, r.text
    detail = client.get("/api/disputes/PP-D-27461").json()
    assert detail["dispute"]["status"] == "UNDER_REVIEW"
    assert detail["dispute"]["evidences"][0]["evidence_type"] == "PROOF_OF_FULFILLMENT"
    # Evidence can't be submitted twice.
    again = client.post("/api/disputes/PP-D-27461/evidence", json={"note": "again"})
    assert again.status_code == 400
    # Sandbox-style ruling closes it as a win.
    client.post("/api/disputes/PP-D-27461/simulate-outcome", json={"outcome": "SELLER_FAVOR"})
    assert client.get("/api/disputes").json()["kpis"]["won"] == 2


def test_offer_and_accept(client):
    r = client.post("/api/disputes/PP-D-27530/offer", json={"note": "Sorry!", "offer_type": "REFUND", "amount": 24.5})
    assert r.status_code == 200, r.text
    d = client.get("/api/disputes/PP-D-27530").json()["dispute"]
    assert d["status"] == "WAITING_FOR_BUYER_RESPONSE" and d["offer"]["seller_offered_amount"]["value"] == "24.50"
    r = client.post("/api/disputes/PP-D-27488/accept", json={"note": "We're sorry - full refund issued."})
    assert r.status_code == 200
    assert client.get("/api/disputes/PP-D-27488").json()["dispute"]["status"] == "RESOLVED"


def test_simulated_dispute_flows_through_webhook_path(client):
    new_id = client.post("/api/simulate-dispute").json()["dispute_id"]
    result = wait_for_analysis(client, new_id)["analysis"]
    assert result["recommendation"] == "OFFER"  # package stuck in transit
    second = client.post("/api/simulate-dispute").json()["dispute_id"]
    fraud = wait_for_analysis(client, second)["analysis"]
    assert fraud["recommendation"] == "ACCEPT"  # re-routed shipment, no seller protection


def test_webhook_endpoint_and_validation(client):
    ok = client.post("/webhooks/paypal", json={"event_type": "CUSTOMER.DISPUTE.UPDATED", "resource": {"dispute_id": "PP-D-27544"}})
    assert ok.status_code == 200
    bad = client.post("/api/disputes/PP-D-27544/offer", json={"note": "x", "offer_type": "BOGUS", "amount": 1})
    assert bad.status_code == 422
    missing = client.get("/api/disputes/PP-D-00000")
    assert missing.status_code == 404


def test_autopilot_guardrails(client):
    client.post("/api/reset")
    client.put("/api/settings", json={"autopilot": True, "autopilot_min_win": 80, "autopilot_max_amount": 100})
    client.post("/api/disputes/PP-D-27461/analyze")  # $86, ~88% -> auto-submitted
    client.post("/api/disputes/PP-D-27502/analyze")  # $219 -> held for a human
    wait_for_analysis(client, "PP-D-27461")
    wait_for_analysis(client, "PP-D-27502")
    time.sleep(0.5)
    assert client.get("/api/disputes/PP-D-27461").json()["dispute"]["status"] == "UNDER_REVIEW"
    assert client.get("/api/disputes/PP-D-27502").json()["dispute"]["status"] == "WAITING_FOR_SELLER_RESPONSE"
    log = " ".join(a["message"] for a in client.get("/api/activity").json())
    assert "Autopilot: Submitted evidence" in log and "held for human review" in log
    client.put("/api/settings", json={"autopilot": False, "autopilot_min_win": 80, "autopilot_max_amount": 150})


# ---- Claude agent loop, exercised against a scripted fake API ---------------------------------


def _msg(stop, blocks):
    return SimpleNamespace(stop_reason=stop, content=blocks, model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def test_agent_loop_runs_tools_and_parses_structured_output(monkeypatch):
    store.load()
    final = {
        "dispute_type": "Item not received - signed delivery", "summary": "s", "recommendation": "FIGHT", "win_probability": 140,
        "reasoning": "r", "key_facts": [], "evidence": [], "tracking_carrier": "USPS", "tracking_number": "9400111202555842761234",
        "refund_id": "", "offer_type": "REFUND", "offer_amount": 0, "response_note": "n", "buyer_message": "b", "risks": [],
        "missing_evidence": [], "process_fix": "",
    }
    script = [
        _msg("tool_use", [
            SimpleNamespace(type="tool_use", id="t1", name="get_dispute", input={"dispute_id": "PP-D-27461"}),
            SimpleNamespace(type="tool_use", id="t2", name="get_tracking", input={"tracking_number": "9400111202555842761234"}),
        ]),
        _msg("end_turn", [SimpleNamespace(type="text", text=json.dumps(final))]),
    ]
    calls = []

    async def fake_create(client, **params):
        calls.append(params)
        return script[len(calls) - 1]

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(agent, "_client", lambda: None)
    from app.mock_paypal import MockPayPal

    steps = []
    result = asyncio.run(agent.investigate("PP-D-27461", ToolContext(MockPayPal(), []), steps.append))
    assert [s["tool"] for s in steps] == ["get_dispute", "get_tracking"]
    assert result["win_probability"] == 100 and result["offer_type"] == "NONE"  # clamped / normalised
    assert result["engine"]["turns"] == 2
    # Both tool results were returned together in a single user turn.
    tool_turn = calls[1]["messages"][2]  # [user prompt, assistant tool_use, user tool_results, ...]
    assert tool_turn["role"] == "user" and len(tool_turn["content"]) == 2
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert all(t["strict"] for t in calls[0]["tools"])
