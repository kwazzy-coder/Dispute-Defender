"""Investigation tools exposed to the Claude agent (and reused by the rule-based fallback)."""

import json
from dataclasses import dataclass

from . import easypost, merchant_data, paypal_data


@dataclass
class ToolContext:
    provider: object          # PayPalClient or MockPayPal
    disputes: list[dict]      # every dispute we know about (for customer dispute history)


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TOOL_DEFS = [
    {
        "name": "get_dispute",
        "description": "Fetch the full PayPal dispute record (reason, lifecycle stage, amount, response deadline, disputed transaction, buyer messages, offers, evidence already submitted, and which seller actions PayPal currently allows).",
        "strict": True,
        "input_schema": _schema({"dispute_id": {"type": "string", "description": "PayPal dispute ID, e.g. PP-D-12345"}}),
    },
    {
        "name": "lookup_paypal_transaction",
        "description": "Look up the disputed payment in PayPal: payer name/email, the ship-to address PayPal has on file, fees, related transactions such as refunds, and the capture's Seller Protection status. Use it to check whether the merchant shipped to the PayPal address (needed for Seller Protection) and whether a refund already exists.",
        "strict": True,
        "input_schema": _schema({
            "transaction_id": {"type": "string", "description": "seller_transaction_id from the dispute"},
            "transaction_time": {"type": "string", "description": "ISO time of the transaction, from the dispute"},
        }),
    },
    {
        "name": "find_order",
        "description": "Find the order record by PayPal capture/transaction ID or invoice number: items, SKU, ship-to address, IP and device, fulfillment carrier and tracking number, return and refund records, internal notes. Uses the store database, or rebuilds the order from PayPal (Payments, Orders and Tracking APIs, including Seller Protection status) when the store has no record.",
        "strict": True,
        "input_schema": _schema({
            "transaction_id": {"type": "string", "description": "PayPal capture/transaction ID, or empty string"},
            "invoice_id": {"type": "string", "description": "Invoice / order number, or empty string"},
        }),
    },
    {
        "name": "get_tracking",
        "description": "Get carrier scan history for a tracking number (outbound shipments or customer returns): status, delivery scan, signature, location.",
        "strict": True,
        "input_schema": _schema({
            "tracking_number": {"type": "string"},
            "carrier": {"type": "string", "description": "Carrier code if known (USPS, UPS, FEDEX, DHL), or empty string"},
        }),
    },
    {
        "name": "search_support_messages",
        "description": "Search the merchant's helpdesk (email/chat) for conversations with this customer. Returns tickets in time order, including messages the store sent.",
        "strict": True,
        "input_schema": _schema({
            "customer_email": {"type": "string"},
            "keywords": {"type": "string", "description": "Optional keywords; empty string returns all tickets for the customer"},
        }),
    },
    {
        "name": "get_customer_history",
        "description": "Customer's order history with this merchant (dates, totals, ship-to addresses, IPs, device IDs) and any disputes involving them. Useful for unauthorized-transaction and duplicate-charge claims.",
        "strict": True,
        "input_schema": _schema({"customer_email": {"type": "string"}}),
    },
    {
        "name": "view_attachment",
        "description": "Open a photo or document so you can look at it: either a helpdesk attachment (pass its file name, e.g. 'bowls-crack-1.png') or a document attached to the PayPal dispute (pass its https URL from get_dispute's attachments). Returns the image itself.",
        "strict": True,
        "input_schema": _schema({"ref": {"type": "string", "description": "Attachment file name or PayPal document URL"}}),
    },
    {
        "name": "get_listing_and_policies",
        "description": "The product listing text for a SKU (what the buyer was promised) plus the store's return, shipping and handmade-variation policies.",
        "strict": True,
        "input_schema": _schema({"sku": {"type": "string", "description": "Product SKU, or empty string for policies only"}}),
    },
]

TOOL_LABELS = {
    "get_dispute": "Reading the PayPal dispute",
    "lookup_paypal_transaction": "Checking PayPal Transaction Search",
    "find_order": "Finding the order in the store database",
    "get_tracking": "Pulling carrier tracking",
    "search_support_messages": "Searching helpdesk conversations",
    "get_customer_history": "Reviewing customer history",
    "get_listing_and_policies": "Reading listing & store policies",
    "view_attachment": "Looking at an attached photo",
}


def _dispute_documents(d: dict) -> list[dict]:
    docs = []
    for source in (d.get("evidences") or []) + (d.get("messages") or []):
        for doc in source.get("documents") or []:
            if doc.get("url"):
                docs.append({"name": doc.get("name"), "url": doc["url"], "from": source.get("source") or source.get("posted_by")})
    return docs


def summarize_dispute(d: dict) -> dict:
    tx = (d.get("disputed_transactions") or [{}])[0]
    return {
        "dispute_id": d.get("dispute_id"),
        "reason": d.get("reason"),
        "status": d.get("status"),
        "lifecycle_stage": d.get("dispute_life_cycle_stage"),
        "channel": d.get("dispute_channel"),
        "amount": d.get("dispute_amount"),
        "created": d.get("create_time"),
        "seller_response_due": d.get("seller_response_due_date"),
        "transaction": {
            "seller_transaction_id": tx.get("seller_transaction_id"),
            "time": tx.get("create_time"),
            "status": tx.get("transaction_status"),
            "gross_amount": tx.get("gross_amount"),
            "invoice_number": tx.get("invoice_number"),
            "buyer_name": (tx.get("buyer") or {}).get("name"),
            "items": tx.get("items"),
        },
        "buyer_and_seller_messages": d.get("messages", []),
        "offer": d.get("offer"),
        "evidence_already_submitted": d.get("evidences", []),
        "attachments": _dispute_documents(d),
        "outcome": d.get("dispute_outcome"),
        "allowed_seller_actions": sorted(
            {(l.get("rel") or "").replace("-", "_") for l in d.get("links", []) or [] if l.get("method") == "POST"}
        ),
    }


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> tuple[dict | list, str]:
    """Run one tool. Returns (result, one-line human summary for the live trace)."""
    if name == "get_dispute":
        d = await ctx.provider.get_dispute(args["dispute_id"])
        s = summarize_dispute(d)
        return s, f"{s['reason']} · {s['amount']['value']} {s['amount']['currency_code']} · {s['lifecycle_stage']}"

    if name == "lookup_paypal_transaction":
        tx = await ctx.provider.search_transaction(args["transaction_id"], args.get("transaction_time") or None)
        if not tx.get("found"):
            return tx, "No Transaction Search record"
        ship = tx.get("paypal_ship_to") or {}
        extra = f", {len(tx['related'])} related txn(s)" if tx.get("related") else ""
        return tx, f"Paid by {tx.get('payer_name')} · PayPal ship-to {ship.get('city')}, {ship.get('state')}{extra}"

    if name == "find_order":
        order = merchant_data.find_order(args.get("transaction_id", ""), args.get("invoice_id", ""))
        source = "store database"
        if not order:
            # No store integration (or a sale outside it): rebuild the order from PayPal itself.
            order = await paypal_data.order_from_paypal(ctx.provider, args.get("transaction_id", ""))
            source = "PayPal"
        if not order:
            return {"found": False}, "No matching order"
        f = order.get("fulfillment") or {}
        return order, f"{order['order_id']} ({source}) · {order['items'][0]['name']} · {f.get('carrier') or '?'} {f.get('tracking_number') or ''}"

    if name == "get_tracking":
        t = merchant_data.get_tracking(args["tracking_number"])
        if not t and easypost.enabled():
            t = await easypost.track(args["tracking_number"], args.get("carrier", ""))
        if not t:
            note = ("No carrier scan data for this number. PayPal stores tracking numbers but not carrier scans; "
                    "set EASYPOST_API_KEY to pull scans from the carrier." if not easypost.enabled() else "Carrier returned nothing.")
            return {"found": False, "note": note}, "No carrier scans available"
        last = t["events"][-1] if t.get("events") else {}
        via = f" via {t['source']}" if t.get("source") else ""
        return t, f"{t['carrier']} {t['status']}{via} · {last.get('description', '')[:70]}"

    if name == "search_support_messages":
        hits = merchant_data.search_support(args["customer_email"], args.get("keywords", ""))
        return {"tickets": hits, "count": len(hits)}, f"{len(hits)} conversation(s) found"

    if name == "get_customer_history":
        h = merchant_data.customer_history(args["customer_email"], ctx.disputes)
        paypal_payments = await paypal_data.payer_history(ctx.provider, args["customer_email"])
        if paypal_payments:
            h["paypal_payments_last_90_days"] = paypal_payments
        extra = f", {len(paypal_payments)} PayPal payment(s) in 90 days" if paypal_payments else ""
        return h, f"{h['order_count']} order(s), ${h['lifetime_spend']} lifetime, {len(h['disputes_involving_customer'])} dispute(s){extra}"

    if name == "view_attachment":
        ref = args["ref"].strip()
        if ref.lower().startswith("http"):
            data, media = await ctx.provider.fetch_document(ref)
            url = None
        else:
            loaded = merchant_data.load_attachment(ref)
            if not loaded:
                raise ValueError(f"No attachment named '{ref}' on any helpdesk ticket.")
            data, media = loaded
            url = f"/api/attachments/{ref}"
        result = {"ref": ref, "media_type": media, "bytes": len(data), "url": url}
        if media in merchant_data.IMAGE_TYPES.values():
            result["image"] = {"media_type": media, "data": merchant_data.b64(data)}
        elif media == "application/pdf":
            result["document"] = {"media_type": media, "data": merchant_data.b64(data)}
        else:
            raise ValueError(f"Unsupported attachment type {media}.")
        return result, f"{ref.rsplit('/', 1)[-1]} ({media}, {len(data) // 1024} KB)"

    if name == "get_listing_and_policies":
        p = merchant_data.listing_and_policies(args.get("sku", ""))
        title = (p.get("listing") or {}).get("title", "policies only")
        return p, f"Listing: {title}"

    raise ValueError(f"Unknown tool {name}")


def to_tool_content(result) -> str:
    return json.dumps(result, default=str)
