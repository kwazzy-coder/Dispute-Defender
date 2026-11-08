"""PayPal REST client (sandbox by default).

Covers everything Dispute Defender needs:
  * Disputes API v1   - list, show, provide-evidence, accept-claim, make-offer, send-message,
                        and the sandbox-only adjudicate endpoint for end-to-end testing
  * Transaction Search - /v1/reporting/transactions for fee/refund/payer enrichment
  * Orders API v2     - create + capture (demo storefront) and add-tracking
  * Webhooks          - verify-webhook-signature

HATEOAS links returned on each dispute are preferred over hard-coded paths, because PayPal only
returns the links for actions that are currently allowed in the dispute's lifecycle stage.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from .config import settings


class PayPalError(Exception):
    def __init__(self, status: int, body: dict | str, action: str) -> None:
        self.status = status
        self.body = body
        self.action = action
        detail = body
        if isinstance(body, dict):
            parts = [body.get("name"), body.get("message")]
            for item in body.get("details", []) or []:
                parts.append(item.get("issue"))
                parts.append(item.get("description"))
            detail = " | ".join(p for p in parts if p)
            if body.get("debug_id"):
                detail += f" (debug_id {body['debug_id']})"
        super().__init__(f"PayPal {action} failed [{status}]: {detail}")


def _rel(link: dict) -> str:
    # PayPal docs spell the same action both ways ("provide-evidence" / "provide_evidence").
    return (link.get("rel") or "").replace("-", "_")


def _find_link(dispute: dict, *rels: str) -> str | None:
    for link in dispute.get("links", []) or []:
        if _rel(link) in rels:
            return link.get("href")
    return None


def _allowed_options(dispute: dict, action: str, key: str) -> list[str] | None:
    return ((dispute.get("allowed_response_options") or {}).get(action) or {}).get(key)


def _paypal_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


class PayPalClient:
    mode = "sandbox"

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(base_url=settings.paypal_base_url, timeout=30.0)
        self._detail_cache: dict[str, tuple[float, dict]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ---- auth & transport -----------------------------------------------------------------

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            resp = await self._http.post(
                "/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(settings.paypal_client_id, settings.paypal_client_secret),
            )
            if resp.status_code != 200:
                raise PayPalError(resp.status_code, _body(resp), "oauth")
            payload = resp.json()
            self._token = payload["access_token"]
            self._token_expiry = time.time() + int(payload.get("expires_in", 3000))
            return self._token

    async def _post_input(self, url: str, action: str, payload: dict, files: dict | None = None) -> dict:
        """POST a dispute action the way the Disputes API documents it: multipart/form-data with the
        JSON body in an `input` part (plus optional files). Falls back to a plain JSON body if the
        endpoint rejects the media type."""
        parts = {"input": (None, json.dumps(payload), "application/json"), **(files or {})}
        try:
            return await self._request("POST", url, action, files=parts)
        except PayPalError as exc:
            if exc.status != 415 or files:
                raise
            return await self._request("POST", url, action, json=payload)

    async def _request(self, method: str, url: str, action: str, **kwargs) -> dict:
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        resp = await self._http.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise PayPalError(resp.status_code, _body(resp), action)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    # ---- disputes --------------------------------------------------------------------------

    async def list_disputes(self) -> list[dict]:
        summaries: list[dict] = []
        params = {"page_size": 50}
        url = "/v1/customer/disputes"
        for _ in range(5):  # up to 250 disputes is plenty for a small merchant
            page = await self._request("GET", url, "list disputes", params=params)
            summaries.extend(page.get("items", []) or [])
            nxt = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)
            if not nxt:
                break
            url, params = nxt, None

        sem = asyncio.Semaphore(5)

        async def detail(item: dict) -> dict:
            async with sem:
                try:
                    return await self.get_dispute(item["dispute_id"])
                except PayPalError:
                    return item  # fall back to the summary row rather than failing the whole list

        return list(await asyncio.gather(*(detail(i) for i in summaries)))

    async def get_dispute(self, dispute_id: str, fresh: bool = False) -> dict:
        cached = self._detail_cache.get(dispute_id)
        if cached and not fresh and time.time() - cached[0] < 20:
            return cached[1]
        data = await self._request("GET", f"/v1/customer/disputes/{dispute_id}", "show dispute")
        self._detail_cache[dispute_id] = (time.time(), data)
        return data

    def _invalidate(self, dispute_id: str) -> None:
        self._detail_cache.pop(dispute_id, None)

    async def provide_evidence(self, dispute_id: str, evidences: list[dict], pdf: bytes, pdf_name: str) -> dict:
        dispute = await self.get_dispute(dispute_id, fresh=True)
        files = {"evidence_file": (pdf_name, pdf, "application/pdf")}
        notes = evidences[0].get("notes", "")[:2000] if evidences else ""
        href = _find_link(dispute, "provide_evidence")
        if href:
            result = await self._post_input(href, "provide evidence", {"evidences": evidences}, files)
            self._invalidate(dispute_id)
            return {"channel": "provide_evidence", "response": result}

        # Chargeback / arbitration stages take a statement + documents instead of typed evidence.
        href = _find_link(dispute, "provide_supporting_info")
        if href:
            result = await self._post_input(href, "provide supporting info", {"notes": notes}, files)
            self._invalidate(dispute_id)
            return {"channel": "provide_supporting_info", "response": result}

        href = _find_link(dispute, "send_message")
        if href:
            result = await self._post_input(href, "send message", {"message": notes}, files)
            self._invalidate(dispute_id)
            return {"channel": "send_message", "response": result}

        allowed = [_rel(l) for l in dispute.get("links", []) if l.get("method") == "POST"]
        raise PayPalError(409, {"message": f"PayPal does not currently allow evidence on this dispute. Allowed actions: {allowed or 'none'}"}, "provide evidence")

    async def accept_claim(self, dispute_id: str, note: str) -> dict:
        dispute = await self.get_dispute(dispute_id, fresh=True)
        href = _find_link(dispute, "accept_claim") or f"/v1/customer/disputes/{dispute_id}/accept-claim"
        body = {"note": note[:2000]}
        types = _allowed_options(dispute, "accept_claim", "accept_claim_types")
        if types and "REFUND" in types:
            body["accept_claim_type"] = "REFUND"
        result = await self._post_input(href, "accept claim", body)
        self._invalidate(dispute_id)
        return result

    async def make_offer(self, dispute_id: str, note: str, offer_type: str, amount: float, currency: str) -> dict:
        dispute = await self.get_dispute(dispute_id, fresh=True)
        allowed = _allowed_options(dispute, "make_offer", "offer_types")
        if allowed and offer_type not in allowed:
            raise ValueError(f"PayPal does not allow a {offer_type} offer on this dispute. Allowed: {', '.join(allowed)}")
        href = _find_link(dispute, "make_offer") or f"/v1/customer/disputes/{dispute_id}/make-offer"
        body: dict = {"note": note[:2000], "offer_type": offer_type}
        if amount and amount > 0:
            body["offer_amount"] = {"currency_code": currency, "value": f"{amount:.2f}"}
        result = await self._request("POST", href, "make offer", json=body)
        self._invalidate(dispute_id)
        return result

    async def send_message(self, dispute_id: str, message: str) -> dict:
        dispute = await self.get_dispute(dispute_id, fresh=True)
        href = _find_link(dispute, "send_message") or f"/v1/customer/disputes/{dispute_id}/send-message"
        result = await self._post_input(href, "send message", {"message": message[:2000]})
        self._invalidate(dispute_id)
        return result

    async def simulate_outcome(self, dispute_id: str, outcome: str) -> dict:
        """Sandbox only: ask PayPal to adjudicate, so the full lifecycle can be demoed."""
        dispute = await self.get_dispute(dispute_id, fresh=True)
        if dispute.get("status") != "UNDER_REVIEW":
            href = _find_link(dispute, "escalate")
            if href:
                await self._request("POST", href, "escalate", json={"note": "Escalating for sandbox adjudication test."})
        result = await self._request(
            "POST",
            f"/v1/customer/disputes/{dispute_id}/adjudicate",
            "adjudicate (sandbox)",
            json={"adjudication_outcome": outcome},
        )
        self._invalidate(dispute_id)
        return result

    async def fetch_document(self, url: str) -> tuple[bytes, str]:
        """Download a document attached to a dispute (buyer photos, receipts).

        Only https URLs on PayPal hosts are allowed and redirects are not followed, so a crafted
        link in a buyer message can't make the agent fetch arbitrary URLs (SSRF)."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in ("paypal.com", "paypalobjects.com")):
            raise ValueError("Only documents hosted by PayPal can be opened.")
        token = await self._access_token()
        resp = await self._http.get(url, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False)
        if resp.status_code >= 400:
            raise PayPalError(resp.status_code, _body(resp), "download document")
        if len(resp.content) > 4_500_000:
            raise ValueError("Document is too large to analyse.")
        return resp.content, resp.headers.get("content-type", "application/octet-stream").split(";")[0]

    # ---- payments / orders / tracking (PayPal-only order data) ------------------------------

    async def get_capture(self, capture_id: str) -> dict:
        """Payments v2 capture: amount, invoice_id, seller_protection, and
        supplementary_data.related_ids.order_id linking back to the checkout order."""
        return await self._request("GET", f"/v2/payments/captures/{capture_id}", "show capture")

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/v2/checkout/orders/{order_id}", "show order")

    async def list_trackers(self, transaction_id: str) -> list[dict]:
        """Tracking API v1: tracking numbers + carrier the merchant added for a capture."""
        data = await self._request("GET", "/v1/shipping/trackers", "list trackers", params={"transaction_id": transaction_id})
        if "trackers" in data:
            return data.get("trackers") or []
        return [data] if data.get("tracking_number") else []

    async def search_transactions_range(self, start: datetime, end: datetime, max_pages: int = 5) -> list[dict]:
        """Transaction Search over one window (PayPal allows at most 31 days per call)."""
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            data = await self._request(
                "GET",
                "/v1/reporting/transactions",
                "transaction search",
                params={
                    "start_date": _paypal_time(start),
                    "end_date": _paypal_time(end),
                    "fields": "transaction_info,payer_info,shipping_info",
                    "page_size": 500,
                    "page": page,
                },
            )
            rows.extend(data.get("transaction_details", []) or [])
            if page >= int(data.get("total_pages", 1) or 1):
                break
        return rows

    # ---- transaction search ----------------------------------------------------------------

    async def search_transaction(self, transaction_id: str, around: str | None) -> dict:
        center = _parse_time(around) or datetime.now(timezone.utc)
        start = center - timedelta(days=2)
        end = min(start + timedelta(days=30), datetime.now(timezone.utc))
        params = {
            "transaction_id": transaction_id,
            "start_date": _paypal_time(start),
            "end_date": _paypal_time(end),
            "fields": "transaction_info,payer_info,shipping_info,cart_info",
        }
        data = await self._request("GET", "/v1/reporting/transactions", "transaction search", params=params)
        details = data.get("transaction_details", []) or []
        seller_protection = None
        try:
            seller_protection = (await self.get_capture(transaction_id)).get("seller_protection")
        except PayPalError:
            pass
        if not details:
            return {
                "found": False,
                "seller_protection": seller_protection,
                "note": "PayPal Transaction Search returned no rows. Sandbox data can lag up to ~3 hours after a payment.",
            }
        row = details[0]
        info = row.get("transaction_info", {})
        payer = row.get("payer_info", {})
        ship = row.get("shipping_info", {})
        name = payer.get("payer_name", {})
        addr = ship.get("address", {})
        return {
            "found": True,
            "transaction_id": info.get("transaction_id"),
            "event_code": info.get("transaction_event_code"),
            "status": info.get("transaction_status"),
            "date": info.get("transaction_initiation_date"),
            "amount": info.get("transaction_amount", {}).get("value"),
            "fee": info.get("fee_amount", {}).get("value"),
            "invoice_id": info.get("invoice_id"),
            "payer_email": payer.get("email_address"),
            "payer_name": " ".join(filter(None, [name.get("given_name"), name.get("surname")])) or name.get("alternate_full_name"),
            "paypal_ship_to": {
                "name": ship.get("name"),
                "line1": addr.get("line1"),
                "city": addr.get("city"),
                "state": addr.get("state"),
                "postal_code": addr.get("postal_code"),
                "country_code": addr.get("country_code"),
            },
            "seller_protection": seller_protection,
            "related": [],
        }

    # ---- webhooks --------------------------------------------------------------------------

    async def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        if not settings.paypal_webhook_id:
            return False
        fields = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": settings.paypal_webhook_id,
        }
        # The signature covers the exact bytes PayPal sent. Splice the raw body in verbatim
        # instead of re-serialising the parsed JSON, which can change number formats or
        # escaping and make a genuine event fail verification.
        prefix = json.dumps(fields)[:-1]
        payload = f'{prefix}, "webhook_event": {raw_body.decode("utf-8")}}}'
        result = await self._request(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            "verify webhook",
            content=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return result.get("verification_status") == "SUCCESS"

    # ---- orders (demo storefront) ----------------------------------------------------------

    async def create_order(self, order_id: str, product: dict, shipping: str) -> dict:
        price = float(product["price"])
        ship = float(shipping)
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": order_id,
                    "invoice_id": order_id,
                    "custom_id": order_id,
                    "description": product["title"],
                    "amount": {
                        "currency_code": "USD",
                        "value": f"{price + ship:.2f}",
                        "breakdown": {
                            "item_total": {"currency_code": "USD", "value": f"{price:.2f}"},
                            "shipping": {"currency_code": "USD", "value": f"{ship:.2f}"},
                        },
                    },
                    "items": [
                        {
                            "name": product["title"],
                            "sku": product["sku"],
                            "quantity": "1",
                            "category": "PHYSICAL_GOODS",
                            "unit_amount": {"currency_code": "USD", "value": f"{price:.2f}"},
                        }
                    ],
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": settings.merchant_name,
                        "shipping_preference": "GET_FROM_FILE",
                        "user_action": "PAY_NOW",
                    }
                }
            },
        }
        return await self._request("POST", "/v2/checkout/orders", "create order", json=body)

    async def capture_order(self, paypal_order_id: str) -> dict:
        return await self._request(
            "POST",
            f"/v2/checkout/orders/{paypal_order_id}/capture",
            "capture order",
            json={},
            headers={"Prefer": "return=representation"},
        )

    async def add_tracking(self, paypal_order_id: str, capture_id: str, tracking_number: str, carrier: str) -> dict:
        body = {"capture_id": capture_id, "tracking_number": tracking_number, "carrier": carrier, "notify_payer": False}
        return await self._request("POST", f"/v2/checkout/orders/{paypal_order_id}/track", "add tracking", json=body)


def _body(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
