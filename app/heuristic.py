"""Deterministic fallback analyst, used when no AI provider is configured.

It walks the same tools in a fixed order and applies simple per-reason rules. It exists so the app
is fully clickable without an API key; the AI agent (app/agent.py) is the real product.
"""

import asyncio
import re
import time

from .tools import TOOL_LABELS, ToolContext, execute_tool


def _same_address(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return norm(a.get("line1")) == norm(b.get("line1")) and norm(a.get("postal_code")) == norm(b.get("postal_code"))


def _money(v) -> str:
    return f"${float(v):.2f}"


async def heuristic_investigate(dispute_id: str, ctx: ToolContext, emit) -> dict:
    trace: list[dict] = []
    started = time.monotonic()

    async def call(name: str, **args):
        t0 = time.monotonic()
        step = {"tool": name, "label": TOOL_LABELS[name], "input": args}
        try:
            result, summary = await execute_tool(name, args, ctx)
            step.update(ok=True, summary=summary)
        except Exception as exc:
            result = None
            step.update(ok=False, summary=str(exc)[:200])
        await asyncio.sleep(0.25)  # pace the live trace so a human can follow it
        step["ms"] = int((time.monotonic() - t0) * 1000)
        trace.append(step)
        emit(step)
        return result

    d = await call("get_dispute", dispute_id=dispute_id)
    tx = d["transaction"]
    amount = float(d["amount"]["value"])
    currency = d["amount"]["currency_code"]
    order = await call("find_order", transaction_id=tx["seller_transaction_id"] or "", invoice_id=tx["invoice_number"] or "")
    order = order if order and order.get("found", True) else None
    pp = await call("lookup_paypal_transaction", transaction_id=tx["seller_transaction_id"] or "", transaction_time=tx["time"] or "")

    out_track = ret_track = None
    if order and order.get("fulfillment"):
        out_track = await call("get_tracking", tracking_number=order["fulfillment"]["tracking_number"])
    if order and order.get("return"):
        ret_track = await call("get_tracking", tracking_number=order["return"]["tracking_number"])
    email = order["customer"]["email"] if order else ""
    tickets = (await call("search_support_messages", customer_email=email, keywords="")) if email else {"tickets": []}
    history = (await call("get_customer_history", customer_email=email)) if email else None
    sku = order["items"][0]["sku"] if order else ""
    listing = await call("get_listing_and_policies", sku=sku)

    facts: list[dict] = []
    evidence: list[dict] = []
    risks: list[str] = []
    missing: list[str] = []
    rec, win, offer_type, offer_amount, fix = "FIGHT", 40, "NONE", 0.0, ""
    carrier = tracking_no = refund_id = ""

    delivered = bool(out_track and out_track.get("found", True) and out_track.get("status") == "DELIVERED")
    signed = bool(out_track and out_track.get("signed_by"))
    paypal_addr = (pp or {}).get("paypal_ship_to") if pp and pp.get("found") else None
    ship_matches = _same_address(order.get("shipping_address") if order else None, paypal_addr)
    ticket_text = " ".join(t["body"].lower() for t in tickets.get("tickets", []) if t.get("from") == "customer")

    if order and order.get("fulfillment"):
        carrier, tracking_no = order["fulfillment"]["carrier"], order["fulfillment"]["tracking_number"]
    if not order:
        missing.append("No matching order found in the store database")
    if paypal_addr is None:
        missing.append("PayPal Transaction Search record (ship-to address) unavailable")

    if out_track and out_track.get("events"):
        last = out_track["events"][-1]
        facts.append({"fact": f"{carrier} {tracking_no}: {last['description']} ({last['time'][:10]})", "source": f"{carrier} tracking", "favors": "SELLER" if delivered else "BUYER"})
    if paypal_addr is not None:
        facts.append({
            "fact": "Shipped to the address on the PayPal transaction" if ship_matches else "Ship-to address differs from the address on the PayPal transaction",
            "source": "PayPal Transaction Search vs order record",
            "favors": "SELLER" if ship_matches else "BUYER",
        })

    reason = d["reason"]
    if reason == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED":
        if delivered:
            rec, win = "FIGHT", 88 if signed and ship_matches else 72 if ship_matches else 45
            evidence.append({"evidence_type": "PROOF_OF_FULFILLMENT", "title": f"{carrier} delivery confirmation", "details": f"Tracking {tracking_no} shows delivery on {out_track['events'][-1]['time'][:10]}.", "source": f"{carrier} tracking"})
            if signed:
                evidence.append({"evidence_type": "PROOF_OF_DELIVERY_SIGNATURE", "title": "Signed for at delivery", "details": f"Signed by {out_track['signed_by']}.", "source": f"{carrier} tracking"})
                facts.append({"fact": f"Delivery was signed for by {out_track['signed_by']}", "source": f"{carrier} tracking", "favors": "SELLER"})
        else:
            rec, win, offer_type = "OFFER", 20, "REPLACEMENT_WITHOUT_REFUND"
            risks.append("Tracking does not show delivery; PayPal usually sides with the buyer on not-received claims without a delivery scan.")
            fix = "Proactively message buyers when tracking stalls for more than 3 days."
    elif reason == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
        damage = any(w in ticket_text for w in ("crack", "broken", "damaged", "shatter")) or any("damaged" in e["description"].lower() for e in (out_track or {}).get("events", []))
        notes = (order or {}).get("internal_notes", "").lower()
        if damage:
            rec, win = "ACCEPT", 12
            facts.append({"fact": "Buyer reported transit damage with photos; carrier logged damage in transit", "source": "Helpdesk / carrier", "favors": "BUYER"})
            if "paper only" in notes or "out of stock" in notes:
                facts.append({"fact": "Internal note: item packed without dividers", "source": "Order notes", "favors": "BUYER"})
            fix = "Never ship multi-piece ceramics without dividers; file a carrier damage claim to recover the cost."
        else:
            handmade = "vary" in (listing.get("listing") or {}).get("description", "").lower()
            if handmade:
                facts.append({"fact": "Listing discloses that colour varies between handmade pieces", "source": "Product listing", "favors": "SELLER"})
            qty_match = re.search(r"set of (\d+)", (order or {}).get("items", [{}])[0].get("name", "").lower())
            per_piece = amount / int(qty_match.group(1)) if qty_match else amount * 0.25
            rec, win, offer_type, offer_amount = "OFFER", 55 if handmade else 35, "REFUND", round(per_piece, 2)
            evidence.append({"evidence_type": "ITEM_DESCRIPTION", "title": "Listing variation disclosure", "details": (listing.get("listing") or {}).get("description", ""), "source": "Product listing"})
            fix = "Show a 'colour varies' photo grid on the listing for handmade sets."
    elif reason == "UNAUTHORISED":
        repeat = history and history["order_count"] > 1 and len({o["device_id"] for o in history["orders"]}) == 1
        if ship_matches and delivered and repeat:
            rec, win = "FIGHT", 76
            evidence.append({"evidence_type": "PROOF_OF_FULFILLMENT", "title": "Delivered to PayPal-confirmed address", "details": f"{carrier} {tracking_no} delivered to the PayPal ship-to address" + (f", signed by {out_track['signed_by']}" if signed else ""), "source": f"{carrier} tracking"})
            evidence.append({"evidence_type": "OTHER", "title": "Established customer", "details": f"{history['order_count']} orders since {history['first_order'][:10]} from the same device and address.", "source": "Customer history"})
            facts.append({"fact": f"Repeat customer: {history['order_count']} orders from the same device and address", "source": "Customer history", "favors": "SELLER"})
            if "gift" in ticket_text or "wrap" in ticket_text:
                facts.append({"fact": "Customer emailed about this order before it shipped", "source": "Helpdesk", "favors": "SELLER"})
        else:
            rec, win = "ACCEPT", 8
            risks.append("Shipped to an address other than the PayPal ship-to address, so Seller Protection is unlikely to apply.")
            fix = "Never change a ship-to address by email; ask the buyer to update it in PayPal and re-pay."
    elif reason == "CREDIT_NOT_PROCESSED":
        refunds = (order or {}).get("refunds", [])
        related = [r for r in (pp or {}).get("related", []) if r.get("event_code") == "T1107"]
        if refunds or related:
            refund_id = (refunds[0]["refund_id"] if refunds else related[0]["transaction_id"])
            rec, win = "FIGHT", 93
            evidence.append({"evidence_type": "PROOF_OF_REFUND", "title": "Refund already issued", "details": f"PayPal refund {refund_id} for {_money(amount)}.", "source": "PayPal / order record"})
            facts.append({"fact": f"Full refund {refund_id} already completed in PayPal", "source": "PayPal Transaction Search", "favors": "SELLER"})
        else:
            rec, win = "ACCEPT", 10
    elif reason == "DUPLICATE_TRANSACTION":
        mine = [o for o in (history or {}).get("orders", []) if o["order_id"] == (order or {}).get("order_id")]
        others = [o for o in (history or {}).get("orders", []) if o["order_id"] != (order or {}).get("order_id")]
        if mine and others and any(o["ship_to"] != mine[0]["ship_to"] for o in others):
            rec, win = "FIGHT", 81
            evidence.append({"evidence_type": "PROOF_OF_FULFILLMENT", "title": "Two separate orders, two shipments", "details": f"{order['order_id']} shipped to {order['shipping_address']['city']}; other order(s): " + ", ".join(f"{o['order_id']} to {o['ship_to']}" for o in others), "source": "Order database"})
            facts.append({"fact": "The two charges are separate orders shipped to different addresses", "source": "Order database", "favors": "SELLER"})
        else:
            rec, win = "ACCEPT", 15

    if rec == "ACCEPT":
        note = f"We have reviewed this case and accept the claim. We are sorry for the trouble and will refund {_money(amount)}."
    elif rec == "OFFER":
        note = "We would like to resolve this directly. " + (
            f"We are offering a partial refund of {_money(offer_amount)} and the buyer keeps the item." if offer_amount else "We will ship a replacement at no cost right away."
        )
    else:
        bullets = "\n".join(f"- {e['title']}: {e['details']}" for e in evidence)
        note = f"The seller disputes this claim. Order {(order or {}).get('order_id', tx['invoice_number'])} for {_money(amount)}.\n{bullets}"

    return {
        "dispute_type": reason.replace("_", " ").title(),
        "summary": f"{tx['buyer_name']} filed a {reason.replace('_', ' ').lower()} dispute for {_money(amount)}.",
        "recommendation": rec,
        "win_probability": win,
        "reasoning": "Rule-based estimate from tracking, PayPal transaction data, order records and helpdesk history. Add an AI key (a free GEMINI_API_KEY, or ANTHROPIC_API_KEY) for a full AI investigation.",
        "key_facts": facts,
        "evidence": evidence if rec == "FIGHT" else [],
        "tracking_carrier": carrier,
        "tracking_number": tracking_no,
        "refund_id": refund_id,
        "offer_type": offer_type,
        "offer_amount": offer_amount,
        "response_note": note[:1800],
        "buyer_message": f"Hi {tx['buyer_name'].split()[0]}, thanks for reaching out about your order. We've reviewed everything and responded through PayPal. - {listing.get('merchant', 'The store')}",
        "risks": risks,
        "missing_evidence": missing,
        "process_fix": fix,
        "buyer_language": "English",
        "engine": {"kind": "rules", "model": "rule-based fallback", "seconds": round(time.monotonic() - started, 1)},
        "trace": trace,
    }
