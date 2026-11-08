"""Wire-level tests for the live PayPal client, using httpx.MockTransport instead of the network."""

import asyncio
import json

import httpx
import pytest

from app.paypal import PayPalClient, PayPalError

DISPUTE = {
    "dispute_id": "PP-D-1",
    "status": "WAITING_FOR_SELLER_RESPONSE",
    "dispute_amount": {"currency_code": "USD", "value": "86.00"},
    "allowed_response_options": {
        "accept_claim": {"accept_claim_types": ["REFUND", "PARTIAL_REFUND"]},
        "make_offer": {"offer_types": ["REFUND"]},
    },
    "links": [
        {"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/provide-evidence", "rel": "provide-evidence", "method": "POST"},
        {"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/accept-claim", "rel": "accept_claim", "method": "POST"},
        {"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/make-offer", "rel": "make_offer", "method": "POST"},
    ],
}


def _multipart_parts(request: httpx.Request) -> dict:
    ctype = request.headers["content-type"]
    assert ctype.startswith("multipart/form-data")
    boundary = ctype.split("boundary=")[1].encode()
    parts = {}
    for chunk in request.content.split(b"--" + boundary):
        if b"Content-Disposition" not in chunk:
            continue
        head, _, body = chunk.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        parts[name] = (head.decode(errors="replace"), body.rstrip(b"\r\n"))
    return parts


@pytest.fixture
def client(monkeypatch):
    from app import paypal as mod

    monkeypatch.setattr(mod.settings.__class__, "paypal_live", property(lambda self: True))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "A21", "expires_in": 32400})
        assert request.headers["authorization"] == "Bearer A21"
        if request.method == "GET" and path == "/v1/customer/disputes/PP-D-1":
            return httpx.Response(200, json=DISPUTE)
        if request.method == "POST":
            return httpx.Response(200, json={"links": [{"rel": "self"}]})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "nope", "debug_id": "abc"})

    c = PayPalClient()
    c._http = httpx.AsyncClient(base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(handler))
    c.seen = seen
    return c


def test_provide_evidence_is_multipart_with_input_json_and_pdf(client):
    evidences = [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "Delivered.", "evidence_info": {"tracking_info": [{"carrier_name": "USPS", "tracking_number": "9400"}]}}]
    result = asyncio.run(client.provide_evidence("PP-D-1", evidences, b"%PDF-1.4 test", "evidence.pdf"))
    assert result["channel"] == "provide_evidence"  # matched the hyphenated rel
    req = client.seen[-1]
    assert req.url.path.endswith("/provide-evidence")
    parts = _multipart_parts(req)
    assert json.loads(parts["input"][1]) == {"evidences": evidences}
    assert "application/json" in parts["input"][0]
    assert 'filename="evidence.pdf"' in parts["evidence_file"][0] and parts["evidence_file"][1] == b"%PDF-1.4 test"


def test_accept_claim_sends_allowed_claim_type(client):
    asyncio.run(client.accept_claim("PP-D-1", "Sorry - refunding in full."))
    parts = _multipart_parts(client.seen[-1])
    assert json.loads(parts["input"][1]) == {"note": "Sorry - refunding in full.", "accept_claim_type": "REFUND"}


def test_make_offer_validates_against_allowed_types(client):
    with pytest.raises(ValueError):
        asyncio.run(client.make_offer("PP-D-1", "n", "REPLACEMENT_WITHOUT_REFUND", 0, "USD"))
    asyncio.run(client.make_offer("PP-D-1", "Partial refund", "REFUND", 24.5, "USD"))
    body = json.loads(client.seen[-1].content)
    assert body == {"note": "Partial refund", "offer_type": "REFUND", "offer_amount": {"currency_code": "USD", "value": "24.50"}}


def test_errors_carry_paypal_debug_id(client):
    with pytest.raises(PayPalError) as err:
        asyncio.run(client._request("GET", "/v1/customer/disputes/missing", "show dispute"))
    assert err.value.status == 404 and "debug_id abc" in str(err.value)
