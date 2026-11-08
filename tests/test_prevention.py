"""Tests for Dispute Radar, impact metrics, carrier claims, PayPal-only order data, EasyPost tracking,
alert webhooks, deadline alerts, and the AI-only endpoints' behaviour without a key."""

import asyncio
import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import easypost, main, paypal_data, service as service_mod
from app.paypal import PayPalClient
from app.tools import ToolContext, execute_tool


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        c.post("/api/reset")
        yield c


# ---- Dispute Radar ---------------------------------------------------------------------------


def test_radar_finds_the_three_at_risk_orders(client):
    client.post("/api/reset")
    items = {i["id"]: i for i in client.get("/api/radar").json()["items"]}
    assert set(items) == {"address_change:KC-2079", "stalled:KC-2077", "unanswered:KC-2080"}
    grace = items["address_change:KC-2079"]
    assert grace["severity"] == "high" and not grace["shipped"]
    assert any("forwarder" in s for s in grace["signals"])
    noah = items["stalled:KC-2077"]
    assert noah["severity"] == "high" and any("PayPal" in s for s in noah["signals"])  # threatened a case
    assert items["unanswered:KC-2080"]["severity"] == "medium"
    # Ordinary, answered or disputed orders never appear.
    assert not any(i["order_id"] in ("KC-2041", "KC-2038", "KC-2072") for i in items.values())


def test_radar_draft_and_actions_resolve_items_and_count_as_prevented(client):
    client.post("/api/reset")
    draft = client.post("/api/radar/address_change:KC-2079/draft").json()
    assert "PayPal" in draft["reply"] and draft["engine"]["kind"] == "rules"  # never agrees to re-route
    assert client.post("/api/radar/address_change:KC-2079/action", json={"action": "hold"}).status_code == 200
    reply = client.post("/api/radar/stalled:KC-2077/draft").json()["reply"]
    assert "9400111202555842762077" in reply
    assert client.post("/api/radar/stalled:KC-2077/action", json={"action": "reply", "reply": reply}).status_code == 200
    assert client.post("/api/radar/unanswered:KC-2080/action", json={"action": "reply", "reply": ""}).status_code == 400
    remaining = [i["id"] for i in client.get("/api/radar").json()["items"]]
    assert remaining == ["unanswered:KC-2080"]
    impact = client.get("/api/disputes").json()["kpis"]["impact"]
    assert impact["disputes_prevented"] == 2 and impact["prevented_amount"] == 253.0
    assert client.post("/api/radar/nope:KC-1/draft").status_code == 404


def test_radar_actions_need_demo_password_when_set(client, monkeypatch):
    client.post("/api/reset")
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, demo_password="pw"))
    assert client.post("/api/radar/address_change:KC-2079/action", json={"action": "hold"}).status_code == 401
    assert client.post("/api/radar/address_change:KC-2079/draft").status_code == 200  # drafting stays open


# ---- impact, claims, AI-only endpoints --------------------------------------------------------


def test_impact_counts_wins_and_avoided_losing_fights(client):
    client.post("/api/reset")
    for did in ("PP-D-27461", "PP-D-27488"):
        client.post(f"/api/disputes/{did}/analyze")
    import time

    deadline = time.time() + 20
    while time.time() < deadline and not all(client.get(f"/api/disputes/{d}").json()["analysis"] for d in ("PP-D-27461", "PP-D-27488")):
        time.sleep(0.3)
    client.post("/api/disputes/PP-D-27461/evidence", json={"note": "Delivered and signed."})
    client.post("/api/disputes/PP-D-27461/simulate-outcome", json={"outcome": "SELLER_FAVOR"})
    client.post("/api/disputes/PP-D-27488/accept", json={"note": "Sorry, refunding."})
    impact = client.get("/api/disputes").json()["kpis"]["impact"]
    assert impact["recovered"] == 86.0
    assert impact["losing_fights_avoided"] == 1 and impact["losing_fights_amount"] == 142.5
    assert impact["hours_saved"] == round(2 * 45 / 60, 1)


def test_carrier_claim_template_for_transit_damage(client):
    claim = client.post("/api/disputes/PP-D-27488/carrier-claim").json()
    assert claim["claim_type"] == "DAMAGE" and claim["carrier"] == "UPS" and claim["claim_amount"] == 142.5
    assert "1Z999AA10123456784" in claim["letter"] and "3 photo" in claim["letter"]


def test_ai_only_endpoints_explain_missing_key(client):
    r = client.post("/api/disputes/PP-D-27461/ask", json={"question": "What if I offer $20?"})
    assert r.status_code == 400 and "GEMINI_API_KEY" in r.json()["error"]
    assert client.post("/api/translate", json={"text": "Hola"}).status_code == 400


# ---- PayPal-only order data --------------------------------------------------------------------


def _paypal(handler) -> PayPalClient:
    c = PayPalClient()
    c._http = httpx.AsyncClient(base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(handler))
    return c


def test_order_rebuilt_from_paypal_capture_order_and_trackers():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        if path == "/v2/payments/captures/CAP1":
            return httpx.Response(200, json={
                "id": "CAP1", "amount": {"currency_code": "USD", "value": "86.00"}, "invoice_id": "KC-3001",
                "seller_protection": {"status": "ELIGIBLE", "dispute_categories": ["ITEM_NOT_RECEIVED"]},
                "supplementary_data": {"related_ids": {"order_id": "ORD9"}}, "create_time": "2026-10-01T10:00:00Z",
            })
        if path == "/v2/checkout/orders/ORD9":
            return httpx.Response(200, json={
                "id": "ORD9", "create_time": "2026-10-01T09:59:00Z",
                "payer": {"name": {"given_name": "Sam", "surname": "Buyer"}, "email_address": "sam@example.com"},
                "purchase_units": [{
                    "items": [{"name": "Tall Speckled Vase", "sku": "KC-VASE-TALL", "quantity": "1", "unit_amount": {"value": "78.00"}}],
                    "shipping": {"name": {"full_name": "Sam Buyer"}, "address": {"address_line_1": "1 Main St", "admin_area_2": "San Jose", "admin_area_1": "CA", "postal_code": "95131", "country_code": "US"}},
                    "payments": {"refunds": []},
                }],
            })
        if path == "/v1/shipping/trackers":
            assert request.url.params["transaction_id"] == "CAP1"
            return httpx.Response(200, json={"trackers": [{"transaction_id": "CAP1", "tracking_number": "EZ4000000004", "carrier": "USPS", "status": "SHIPPED", "shipment_date": "2026-10-02"}]})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})

    order = asyncio.run(paypal_data.order_from_paypal(_paypal(handler), "CAP1"))
    assert order["source"].startswith("PayPal") and order["paypal_order_id"] == "ORD9"
    assert order["customer"] == {"name": "Sam Buyer", "email": "sam@example.com"}
    assert order["shipping_address"]["city"] == "San Jose" and order["items"][0]["sku"] == "KC-VASE-TALL"
    assert order["fulfillment"]["tracking_number"] == "EZ4000000004"
    assert order["seller_protection"]["status"] == "ELIGIBLE"


def test_payer_history_filters_transaction_search_by_email():
    windows = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        windows.append((request.url.params["start_date"], request.url.params["end_date"]))
        return httpx.Response(200, json={"total_pages": 1, "transaction_details": [
            {"transaction_info": {"transaction_id": "A", "transaction_amount": {"value": "10.00"}}, "payer_info": {"email_address": "Sam@Example.com"}},
            {"transaction_info": {"transaction_id": "B"}, "payer_info": {"email_address": "other@example.com"}},
        ]})

    hist = asyncio.run(paypal_data.payer_history(_paypal(handler), "sam@example.com", days=90))
    assert len(windows) == 3  # three 30-day windows (the API allows at most 31 days per call)
    assert [h["transaction_id"] for h in hist] == ["A", "A", "A"]


# ---- EasyPost ----------------------------------------------------------------------------------


def test_easypost_tracking_is_normalised_and_used_by_the_agent_tool(monkeypatch):
    monkeypatch.setattr(easypost, "settings", dataclasses.replace(easypost.settings, easypost_api_key="EZTKtest"))
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["auth"] = request.headers["authorization"]
        sent["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "id": "trk_1", "mode": "test", "tracking_code": "EZ4000000004", "carrier": "USPS", "status": "delivered",
            "signed_by": "John Tester", "public_url": "https://track.easypost.com/x",
            "tracking_details": [
                {"message": "Pre-Shipment information received", "status": "pre_transit", "datetime": "2026-10-01T10:00:00Z", "tracking_location": {"city": None}},
                {"message": "Delivered", "status": "delivered", "datetime": "2026-10-03T15:00:00Z", "tracking_location": {"city": "CHARLESTON", "state": "SC", "zip": "29401"}},
            ],
        })

    easypost._cache.clear()
    monkeypatch.setattr(easypost, "_http", httpx.AsyncClient(base_url=easypost.API, auth=("EZTKtest", ""), transport=httpx.MockTransport(handler)))
    result, summary = asyncio.run(execute_tool("get_tracking", {"tracking_number": "EZ4000000004", "carrier": "USPS"}, ToolContext(None, [])))
    assert sent["body"] == {"tracker": {"tracking_code": "EZ4000000004", "carrier": "USPS"}}
    assert sent["auth"].startswith("Basic ")
    assert result["status"] == "DELIVERED" and result["signed_by"] == "John Tester"
    assert result["events"][-1] == {"time": "2026-10-03T15:00:00Z", "location": "CHARLESTON, SC, 29401", "description": "Delivered"}
    assert "EasyPost (test mode)" in summary


def test_tracking_without_easypost_explains_paypal_limitation():
    result, _ = asyncio.run(execute_tool("get_tracking", {"tracking_number": "UNKNOWN123", "carrier": ""}, ToolContext(None, [])))
    assert result["found"] is False and "EASYPOST_API_KEY" in result["note"]


# ---- alerts ------------------------------------------------------------------------------------


def test_alert_webhook_and_deadline_alerts_fire_once(client, monkeypatch):
    client.post("/api/reset")
    posts = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json):
            posts.append((url, json))
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(service_mod, "settings", dataclasses.replace(service_mod.settings, alert_webhook_url="https://hooks.example/x"))
    monkeypatch.setattr(service_mod.httpx, "AsyncClient", FakeClient)
    svc = service_mod.service

    async def run():
        first = await svc.check_deadlines()
        second = await svc.check_deadlines()
        return first, second

    first, second = asyncio.run(run())
    assert first == ["PP-D-27502"] and second == []  # due in < 48h, alerted exactly once
    assert posts[0][0] == "https://hooks.example/x" and "PP-D-27502" in posts[0][1]["text"] and posts[0][1]["event"] == "deadline"
