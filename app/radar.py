"""Dispute Radar: spot orders likely to turn into PayPal disputes, before the buyer files one.

Signals (deterministic, so they're explainable and testable):
  * stalled parcel   - carrier status still in transit with no scan for 3+ days
  * unanswered buyer - the customer's latest message has no store reply after 6+ hours
  * address change   - the buyer asks by email to ship somewhere else (a classic account-takeover
                       pattern; PayPal Seller Protection generally needs the PayPal ship-to address)
  * forwarder        - the order ships to a parcel-forwarding address

The AI (Claude or Gemini) drafts the preventive reply in the customer's language; the merchant sends
it, holds the order, or reships. Nothing is sent without a click.
"""

import re
from datetime import datetime, timedelta, timezone

from . import merchant_data
from .store import iso, store, utcnow

STALL_DAYS = 3
UNANSWERED_HOURS = 6
LOOKBACK_DAYS = 45
ADDRESS_CHANGE = re.compile(
    r"\b(ship|send|deliver)\w*\b.{0,60}\b(to my|to a|instead|another|different)\b|change (the )?(shipping )?address|new address",
    re.IGNORECASE | re.DOTALL,
)
FORWARDER = re.compile(r"\b(forward(ing|er)?|fwd|reshipper|c/o)\b", re.IGNORECASE)
ESCALATION = re.compile(r"\b(paypal|dispute|chargeback|refund|case|reembolso|disputa)\b", re.IGNORECASE)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_hours(ts: str | None, now: datetime) -> float | None:
    t = _parse(ts)
    return (now - t).total_seconds() / 3600 if t else None


def scan(disputes: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    resolved = store.state.setdefault("radar_resolved", {})
    disputed = {
        t.get("invoice_number")
        for d in disputes
        for t in d.get("disputed_transactions", []) or []
    }
    items = []
    for order in store.merchant["orders"]:
        placed = _parse(order.get("placed"))
        if not placed or now - placed > timedelta(days=LOOKBACK_DAYS) or order["invoice_id"] in disputed:
            continue
        signals, kinds = [], []
        tickets = merchant_data.search_support(order["customer"]["email"])
        last_customer = next((t for t in reversed(tickets) if t["from"] == "customer"), None)
        answered = last_customer and any(t["from"] == "store" and t.get("time", "") > last_customer.get("time", "") for t in tickets)

        f = order.get("fulfillment") or {}
        track = merchant_data.get_tracking(f["tracking_number"]) if f.get("tracking_number") else None
        if track and track.get("status") in ("IN_TRANSIT", "PRE_TRANSIT") and track.get("events"):
            idle = _age_hours(track["events"][-1]["time"], now) or 0
            if idle >= STALL_DAYS * 24:
                kinds.append("stalled")
                signals.append(f"No carrier scan for {idle / 24:.1f} days ({track['events'][-1]['description']})")

        if last_customer and not answered:
            waited = _age_hours(last_customer.get("time"), now) or 0
            if waited >= UNANSWERED_HOURS:
                kinds.append("unanswered")
                signals.append(f"Customer waiting {waited:.0f}h for a reply: \"{last_customer.get('subject', '')}\"")
                if ESCALATION.search(last_customer.get("body", "")):
                    signals.append("Customer mentions PayPal / a case / a refund")

        change_ticket = next((t for t in tickets if t["from"] == "customer" and ADDRESS_CHANGE.search(t.get("body", ""))), None)
        if change_ticket:
            kinds.append("address_change")
            signals.append("Buyer asked by email to ship to a different address")
            if FORWARDER.search(change_ticket.get("body", "")):
                signals.append("Requested address is a parcel forwarder")
            if not f:
                signals.append("Order not shipped yet - still time to stop it")
        if FORWARDER.search(" ".join(str(v) for v in (order.get("shipping_address") or {}).values())):
            kinds.append("address_change")
            signals.append("Ship-to is a parcel-forwarding address")

        if not kinds:
            continue
        kind = "address_change" if "address_change" in kinds else "stalled" if "stalled" in kinds else "unanswered"
        item_id = f"{kind}:{order['order_id']}"
        if item_id in resolved:
            continue
        amount = float(order["total"])
        severity = "high" if kind == "address_change" or (kind == "stalled" and "unanswered" in kinds) or any("PayPal" in s for s in signals) else "medium"
        items.append({
            "id": item_id,
            "kind": kind,
            "severity": severity,
            "order_id": order["order_id"],
            "customer": order["customer"]["name"],
            "email": order["customer"]["email"],
            "item": ", ".join(i["name"] for i in order["items"]),
            "amount": amount,
            "currency": order.get("currency", "USD"),
            "title": {
                "address_change": "Possible account takeover: address-change request",
                "stalled": "Parcel stalled in transit",
                "unanswered": "Customer waiting for a reply",
            }[kind],
            "signals": signals,
            "suggested_action": {
                "address_change": "Hold the order and ask the buyer to change the address in PayPal",
                "stalled": "Reply with tracking facts, chase the carrier, offer a reship",
                "unanswered": "Reply now, before they open a PayPal case",
            }[kind],
            "last_message": last_customer.get("body") if last_customer else "",
            "ticket_id": (change_ticket or last_customer or {}).get("id"),
            "shipped": bool(f),
            "on_hold": bool(order.get("hold")),
        })
    order_rank = {"high": 0, "medium": 1}
    return sorted(items, key=lambda i: (order_rank[i["severity"]], -i["amount"]))


def context_for(item: dict) -> dict:
    order = next(o for o in store.merchant["orders"] if o["order_id"] == item["order_id"])
    f = order.get("fulfillment") or {}
    return {
        "store": store.merchant["name"],
        "risk": {k: item[k] for k in ("kind", "title", "signals", "suggested_action")},
        "order": {k: order.get(k) for k in ("order_id", "placed", "items", "total", "currency", "shipping_address", "fulfillment")},
        "tracking": merchant_data.get_tracking(f["tracking_number"]) if f.get("tracking_number") else None,
        "conversation": merchant_data.search_support(order["customer"]["email"]),
        "policies": store.merchant["policies"],
    }


def template_reply(item: dict) -> dict:
    """Rule-based fallback when no AI provider is configured (English only)."""
    ctx = context_for(item)
    first = item["customer"].split()[0]
    t = ctx["tracking"] or {}
    last = (t.get("events") or [{}])[-1]
    shop = store.merchant["name"]
    if item["kind"] == "address_change":
        body = (f"Hi {first}, thanks for your order. For your security we can only ship to the address on your PayPal "
                f"payment. If you'd like it delivered elsewhere, please update the shipping address in your PayPal "
                f"account (or cancel and re-order with the new address) and we'll ship right away. Your order is on "
                f"hold until then. - {shop}")
        action = "Keep the order on hold until the buyer confirms through PayPal."
    elif item["kind"] == "stalled":
        body = (f"Hi {first}, I'm sorry your {item['item']} is taking longer than expected. {t.get('carrier', '')} "
                f"tracking {t.get('tracking_number', '')} last updated {last.get('time', '')[:10]}: {last.get('description', '')}. "
                f"We've asked the carrier to investigate, and if it hasn't moved in 2 days we'll ship a replacement at no cost. - {shop}")
        action = "Open a carrier trace; reship if no scan in 48h."
    else:
        body = (f"Hi {first}, thanks for your message and sorry for the wait. {t.get('carrier', 'The carrier')} shows your "
                f"parcel as {str(t.get('status', 'shipped')).lower().replace('_', ' ')} ({last.get('description', '')}, "
                f"{last.get('time', '')[:10]}). Could you check with neighbours or your building's mail room? If it hasn't "
                f"turned up in 2 days, reply here and we'll make it right. - {shop}")
        action = ""
    return {
        "language": "English",
        "reply": body,
        "reply_english": body,
        "customer_message_english": item.get("last_message", ""),
        "internal_action": action,
        "engine": {"kind": "rules", "note": "Add an AI key (e.g. a free GEMINI_API_KEY) to reply in the customer's language."},
    }


async def resolve(item: dict, action: str, reply: str = "") -> str:
    order = next(o for o in store.merchant["orders"] if o["order_id"] == item["order_id"])
    now = iso(utcnow())
    if action == "reply":
        store.merchant["helpdesk"].append({
            "id": f"T-R{len(store.merchant['helpdesk']) + 1}",
            "customer_email": item["email"],
            "time": now,
            "from": "store",
            "channel": "email",
            "subject": f"Re: your order {item['order_id']}",
            "body": reply,
        })
        summary = f"Replied to {item['customer']} about {item['order_id']}"
    elif action == "hold":
        order["hold"] = True
        order["internal_notes"] = (order.get("internal_notes", "") + " ON HOLD: address-change request pending PayPal confirmation.").strip()
        summary = f"Put {item['order_id']} on hold (suspicious address change)"
    elif action == "reship":
        order["internal_notes"] = (order.get("internal_notes", "") + f" Replacement shipped {now[:10]}.").strip()
        summary = f"Shipped a replacement for {item['order_id']}"
    elif action == "dismiss":
        summary = f"Dismissed radar alert on {item['order_id']}"
    else:
        raise ValueError("Unknown action")
    store.state.setdefault("radar_resolved", {})[item["id"]] = {"action": action, "time": now, "amount": item["amount"]}
    await store.save()
    return summary
