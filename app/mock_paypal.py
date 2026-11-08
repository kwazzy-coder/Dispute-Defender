"""Offline stand-in for the PayPal Disputes API, used when no sandbox credentials are configured.

It returns objects in the same shape as the real API (including HATEOAS `links` that depend on the
dispute status), so every other module runs unchanged in demo mode.
"""

import random
import string

from .seed import resolve_dates
from .store import iso, store, utcnow

BASE = "https://api-m.sandbox.paypal.com/v1/customer/disputes"


def _links(dispute: dict) -> list[dict]:
    did = dispute["dispute_id"]
    links = [{"href": f"{BASE}/{did}", "rel": "self", "method": "GET"}]
    if dispute["status"] == "WAITING_FOR_SELLER_RESPONSE":
        for rel, path in (
            ("provide_evidence", "provide-evidence"),
            ("accept_claim", "accept-claim"),
            ("make_offer", "make-offer"),
            ("send_message", "send-message"),
        ):
            links.append({"href": f"{BASE}/{did}/{path}", "rel": rel, "method": "POST"})
    elif dispute["status"] in ("UNDER_REVIEW", "WAITING_FOR_BUYER_RESPONSE"):
        links.append({"href": f"{BASE}/{did}/send-message", "rel": "send_message", "method": "POST"})
    return links


def _paypal_id(length: int = 17) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


class MockPayPal:
    mode = "mock"

    def _disputes(self) -> list[dict]:
        return store.state["mock_disputes"]

    def _find(self, dispute_id: str) -> dict:
        for dispute in self._disputes():
            if dispute["dispute_id"] == dispute_id:
                return dispute
        raise KeyError(dispute_id)

    def _view(self, dispute: dict) -> dict:
        return {**dispute, "links": _links(dispute)}

    async def close(self) -> None:
        return None

    async def list_disputes(self) -> list[dict]:
        return [self._view(d) for d in self._disputes()]

    async def get_dispute(self, dispute_id: str, fresh: bool = False) -> dict:
        return self._view(self._find(dispute_id))

    async def _touch(self, dispute: dict, status: str | None = None) -> None:
        if status:
            dispute["status"] = status
        dispute["update_time"] = iso(utcnow())
        await store.save()

    async def provide_evidence(self, dispute_id: str, evidences: list[dict], pdf: bytes, pdf_name: str) -> dict:
        dispute = self._find(dispute_id)
        if dispute["status"] != "WAITING_FOR_SELLER_RESPONSE":
            raise ValueError(f"Dispute is {dispute['status']}; PayPal only accepts evidence while waiting for the seller.")
        dispute.setdefault("evidences", []).extend(
            {**e, "date": iso(utcnow()), "documents": [{"name": pdf_name}]} for e in evidences
        )
        await self._touch(dispute, "UNDER_REVIEW")
        return {"channel": "provide_evidence", "response": {"links": [{"rel": "self", "href": f"{BASE}/{dispute_id}"}]}}

    async def accept_claim(self, dispute_id: str, note: str) -> dict:
        dispute = self._find(dispute_id)
        dispute["dispute_outcome"] = {"outcome_code": "ACCEPTED", "amount_refunded": dispute["dispute_amount"]}
        dispute.setdefault("messages", []).append({"posted_by": "SELLER", "time_posted": iso(utcnow()), "content": note})
        await self._touch(dispute, "RESOLVED")
        return {"status": "RESOLVED"}

    async def make_offer(self, dispute_id: str, note: str, offer_type: str, amount: float, currency: str) -> dict:
        dispute = self._find(dispute_id)
        dispute["offer"] = {
            "offer_type": offer_type,
            "seller_offered_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
            "history": [{"offer_time": iso(utcnow()), "actor": "SELLER", "event_type": "PROPOSED", "offer_type": offer_type}],
        }
        dispute.setdefault("messages", []).append({"posted_by": "SELLER", "time_posted": iso(utcnow()), "content": note})
        await self._touch(dispute, "WAITING_FOR_BUYER_RESPONSE")
        return {"status": "WAITING_FOR_BUYER_RESPONSE"}

    async def buyer_accepts_offer(self, dispute_id: str) -> None:
        dispute = self._find(dispute_id)
        if dispute["status"] != "WAITING_FOR_BUYER_RESPONSE" or "offer" not in dispute:
            return
        dispute["offer"]["history"].append({"offer_time": iso(utcnow()), "actor": "BUYER", "event_type": "ACCEPTED"})
        dispute["dispute_outcome"] = {"outcome_code": "ACCEPTED", "amount_refunded": dispute["offer"]["seller_offered_amount"]}
        await self._touch(dispute, "RESOLVED")

    async def send_message(self, dispute_id: str, message: str) -> dict:
        dispute = self._find(dispute_id)
        dispute.setdefault("messages", []).append({"posted_by": "SELLER", "time_posted": iso(utcnow()), "content": message})
        await self._touch(dispute)
        return {"status": dispute["status"]}

    async def simulate_outcome(self, dispute_id: str, outcome: str) -> dict:
        dispute = self._find(dispute_id)
        dispute["dispute_outcome"] = {
            "outcome_code": "RESOLVED_SELLER_FAVOUR" if outcome == "SELLER_FAVOR" else "RESOLVED_BUYER_FAVOUR"
        }
        await self._touch(dispute, "RESOLVED")
        return {"status": "RESOLVED"}

    async def search_transaction(self, transaction_id: str, around: str | None) -> dict:
        tx = store.state["mock_transactions"].get(transaction_id)
        if not tx:
            return {"found": False, "note": "No PayPal transaction with that ID in this account."}
        return {
            "found": True,
            "transaction_id": transaction_id,
            "event_code": "T0006",
            "status": "S",
            "date": tx["date"],
            "amount": tx["amount"],
            "fee": tx["fee"],
            "invoice_id": tx["invoice_id"],
            "payer_email": tx["payer_email"],
            "payer_name": tx["payer_name"],
            "paypal_ship_to": tx["ship_to"],
            # Same shape as Payments v2 capture.seller_protection. Eligibility is decided at payment
            # time; whether it holds still depends on shipping to the address above with proof.
            "seller_protection": {"status": "ELIGIBLE", "dispute_categories": ["ITEM_NOT_RECEIVED", "UNAUTHORIZED_TRANSACTION"]},
            "related": tx.get("related", []),
        }

    async def fetch_document(self, url: str) -> tuple[bytes, str]:
        raise ValueError("Demo mode has no PayPal-hosted dispute documents; use helpdesk attachments instead.")

    async def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        return True  # demo mode accepts locally simulated webhook events

    async def simulate_new_dispute(self) -> dict:
        """Create a brand-new dispute (plus the merchant-side records behind it) to demo the webhook flow."""
        templates = store.state["simulations"]
        index = store.state["mock_counter"] % len(templates)
        store.state["mock_counter"] += 1
        capture_id = _paypal_id()
        suffix = str(27600 + store.state["mock_counter"])
        sim = resolve_dates(templates[index], replacements={"capture_id": capture_id})

        merchant = store.merchant
        order = sim["order"]
        if store.state["mock_counter"] > len(templates):
            # Re-running a template: give the order a unique number so lookups stay unambiguous.
            order["order_id"] = order["invoice_id"] = f"{order['order_id']}-{store.state['mock_counter']}"
            sim["transaction"]["invoice_id"] = order["invoice_id"]
            sim["dispute"]["disputed_transactions"][0]["invoice_number"] = order["invoice_id"]
        merchant["orders"].append(order)
        merchant["tracking"].update(sim["tracking"])
        for i, ticket in enumerate(sim["helpdesk"]):
            merchant["helpdesk"].append({"id": f"T-{suffix}-{i}", **ticket})
        store.state["mock_transactions"][capture_id] = sim["transaction"]

        now = iso(utcnow())
        dispute = {"dispute_id": f"PP-D-{suffix}", "create_time": now, "update_time": now, **sim["dispute"]}
        self._disputes().insert(0, dispute)
        await store.save()
        return self._view(dispute)
