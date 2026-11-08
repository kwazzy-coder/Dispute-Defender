"""PayPal-only order data: rebuild the merchant's order record from PayPal itself.

For a merchant with no store integration, everything the agent needs about the sale can come from
PayPal (sandbox mode only - demo mode uses the seeded store database):

  capture   GET /v2/payments/captures/{id}   amount, invoice_id, seller_protection, -> order_id
  order     GET /v2/checkout/orders/{id}     items, ship-to address, payer, refunds
  trackers  GET /v1/shipping/trackers        tracking number + carrier the merchant added
  history   GET /v1/reporting/transactions   the payer's other payments (filtered by email)

PayPal does not expose carrier scan events, so delivery proof comes from EasyPost when configured.
"""

from datetime import datetime, timedelta, timezone

from .paypal import PayPalError

SOURCE = "PayPal (Payments, Orders and Tracking APIs)"


def _money(m: dict | None) -> str:
    return (m or {}).get("value", "0.00")


async def order_from_paypal(provider, capture_id: str) -> dict | None:
    if getattr(provider, "mode", "") != "sandbox" or not capture_id:
        return None
    try:
        capture = await provider.get_capture(capture_id)
    except PayPalError:
        return None
    order_id = ((capture.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
    order: dict = {}
    if order_id:
        try:
            order = await provider.get_order(order_id)
        except PayPalError:
            order = {}  # orders created by another app may not be readable; the capture still helps
    unit = (order.get("purchase_units") or [{}])[0]
    shipping = unit.get("shipping") or {}
    addr = shipping.get("address") or {}
    payer = order.get("payer") or {}
    pname = payer.get("name") or {}
    trackers = []
    try:
        trackers = await provider.list_trackers(capture_id)
    except PayPalError:
        pass
    tracker = next((t for t in trackers if (t.get("status") or "").upper() != "CANCELLED"), None)
    refunds = [
        {
            "refund_id": r.get("id"),
            "amount": _money(r.get("amount")),
            "currency": (r.get("amount") or {}).get("currency_code", "USD"),
            "issued": r.get("create_time"),
            "status": r.get("status"),
        }
        for r in ((unit.get("payments") or {}).get("refunds") or [])
    ]
    return {
        "source": SOURCE,
        "order_id": order_id or capture_id,
        "invoice_id": capture.get("invoice_id") or unit.get("invoice_id"),
        "paypal_order_id": order_id,
        "paypal_capture_id": capture_id,
        "placed": order.get("create_time") or capture.get("create_time"),
        "customer": {
            "name": " ".join(filter(None, [pname.get("given_name"), pname.get("surname")])) or (shipping.get("name") or {}).get("full_name", ""),
            "email": payer.get("email_address", ""),
        },
        "items": [
            {"sku": i.get("sku", ""), "name": i.get("name", ""), "qty": int(i.get("quantity", 1) or 1), "price": _money(i.get("unit_amount"))}
            for i in unit.get("items") or []
        ] or [{"sku": "", "name": unit.get("description") or "Item", "qty": 1, "price": _money(capture.get("amount"))}],
        "total": _money(capture.get("amount")),
        "currency": (capture.get("amount") or {}).get("currency_code", "USD"),
        "shipping_address": {
            "name": (shipping.get("name") or {}).get("full_name", ""),
            "line1": addr.get("address_line_1", ""),
            "city": addr.get("admin_area_2", ""),
            "state": addr.get("admin_area_1", ""),
            "postal_code": addr.get("postal_code", ""),
            "country_code": addr.get("country_code", ""),
        },
        "fulfillment": {
            "carrier": tracker.get("carrier"),
            "tracking_number": tracker.get("tracking_number"),
            "shipped": tracker.get("shipment_date"),
            "paypal_tracker_status": tracker.get("status"),
        } if tracker else None,
        "refunds": refunds,
        "seller_protection": capture.get("seller_protection"),
        "internal_notes": "",
    }


async def payer_history(provider, email: str, days: int = 90) -> list[dict]:
    """The payer's payments to this merchant in the last `days`, via Transaction Search.

    The API has no payer-email filter and allows 31 days per call, so we page through windows and
    filter locally."""
    if getattr(provider, "mode", "") != "sandbox" or not email:
        return []
    end = datetime.now(timezone.utc)
    out: list[dict] = []
    while days > 0:
        span = min(days, 30)
        start = end - timedelta(days=span)
        try:
            rows = await provider.search_transactions_range(start, end)
        except PayPalError:
            break
        for row in rows:
            payer = row.get("payer_info") or {}
            if (payer.get("email_address") or "").lower() != email.lower():
                continue
            info = row.get("transaction_info") or {}
            ship = (row.get("shipping_info") or {}).get("address") or {}
            out.append({
                "transaction_id": info.get("transaction_id"),
                "date": info.get("transaction_initiation_date"),
                "event_code": info.get("transaction_event_code"),
                "amount": (info.get("transaction_amount") or {}).get("value"),
                "status": info.get("transaction_status"),
                "ship_to": ", ".join(filter(None, [ship.get("line1"), ship.get("city"), ship.get("state"), ship.get("postal_code")])),
            })
        end, days = start, days - span
    return out
