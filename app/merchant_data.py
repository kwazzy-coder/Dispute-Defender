"""Read-only views over the merchant's own systems: order database, carrier tracking, helpdesk, policies.

In production these would be Shopify/WooCommerce, a tracking aggregator and a helpdesk such as
Zendesk. Here they are backed by the JSON store so the demo is self-contained; orders placed through
the built-in sandbox storefront are written to the same place.
"""

import base64
from pathlib import Path

from .config import ROOT
from .store import store

ATTACHMENTS_DIR = ROOT / "data" / "attachments"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_ATTACHMENT_BYTES = 4_500_000  # under the API's per-image limit


def _orders() -> list[dict]:
    return store.merchant["orders"]


def find_order(transaction_id: str = "", invoice_id: str = "") -> dict | None:
    for order in _orders():
        if transaction_id and order.get("paypal_capture_id") == transaction_id:
            return order
        if invoice_id and order.get("invoice_id") == invoice_id:
            return order
    return None


def get_tracking(tracking_number: str) -> dict | None:
    record = store.merchant["tracking"].get(tracking_number)
    if not record:
        return None
    return {"tracking_number": tracking_number, **record}


def search_support(customer_email: str, keywords: str = "") -> list[dict]:
    email = customer_email.strip().lower()
    terms = [t for t in keywords.lower().split() if len(t) > 2]
    hits = []
    for ticket in store.merchant["helpdesk"]:
        if email and ticket["customer_email"].lower() != email:
            continue
        text = f"{ticket.get('subject', '')} {ticket.get('body', '')}".lower()
        if terms and email == "" and not any(t in text for t in terms):
            continue
        hits.append(ticket)
    return sorted(hits, key=lambda t: t.get("time", ""))


def customer_history(customer_email: str, disputes: list[dict]) -> dict:
    email = customer_email.strip().lower()
    orders = [o for o in _orders() if o["customer"]["email"].lower() == email]
    invoices = {o["invoice_id"] for o in orders}
    prior = [
        {
            "dispute_id": d["dispute_id"],
            "reason": d.get("reason"),
            "status": d.get("status"),
            "outcome": (d.get("dispute_outcome") or {}).get("outcome_code"),
        }
        for d in disputes
        if any(t.get("invoice_number") in invoices for t in d.get("disputed_transactions", []) or [])
    ]
    return {
        "customer_email": customer_email,
        "order_count": len(orders),
        "lifetime_spend": f"{sum(float(o['total']) for o in orders):.2f}",
        "first_order": min((o["placed"] for o in orders), default=None),
        "orders": [
            {
                "order_id": o["order_id"],
                "placed": o["placed"],
                "total": o["total"],
                "ship_to": _addr(o["shipping_address"]),
                "ip": o.get("ip"),
                "device_id": o.get("device_id"),
            }
            for o in sorted(orders, key=lambda o: o["placed"])
        ],
        "disputes_involving_customer": prior,
    }


def listing_and_policies(sku: str = "") -> dict:
    products = store.merchant["products"]
    listing = next((p for p in products if p["sku"] == sku), None) if sku else None
    return {
        "merchant": store.merchant["name"],
        "website": store.merchant.get("website"),
        "listing": listing,
        "policies": store.merchant["policies"],
    }


def attachment_names() -> set[str]:
    return {name for t in store.merchant["helpdesk"] for name in t.get("attachments", []) or []}


def load_attachment(name: str) -> tuple[bytes, str] | None:
    """Return (bytes, media type) for a helpdesk attachment.

    Only files that a helpdesk ticket actually references are served, and only by bare file name,
    so neither a browser nor a prompt-injected agent can read arbitrary files from disk.
    """
    safe = Path(name).name
    if safe != name or safe not in attachment_names():
        return None
    path = ATTACHMENTS_DIR / safe
    media = IMAGE_TYPES.get(path.suffix.lower()) or ("application/pdf" if path.suffix.lower() == ".pdf" else None)
    if not media or not path.is_file() or path.stat().st_size > MAX_ATTACHMENT_BYTES:
        return None
    return path.read_bytes(), media


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def _addr(a: dict) -> str:
    return ", ".join(filter(None, [a.get("name"), a.get("line1"), a.get("city"), a.get("state"), a.get("postal_code")]))
