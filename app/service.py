"""Orchestration: analysis runs, human-approved PayPal actions, autopilot, webhooks, storefront."""

import asyncio
import random
import string
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import agent, easypost, merchant_data, paypal_data, radar
from .config import settings
from .events import bus
from .evidence import build_evidences, build_pdf
from .heuristic import heuristic_investigate
from .mock_paypal import MockPayPal
from .paypal import PayPalClient
from .store import iso, store, utcnow
from .tools import ToolContext, summarize_dispute

REASON_LABELS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Item not received",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Not as described",
    "UNAUTHORISED": "Unauthorized",
    "CREDIT_NOT_PROCESSED": "Refund not processed",
    "DUPLICATE_TRANSACTION": "Duplicate charge",
    "INCORRECT_AMOUNT": "Incorrect amount",
    "PAYMENT_BY_OTHER_MEANS": "Paid by other means",
    "CANCELED_RECURRING_BILLING": "Cancelled subscription",
    "PROBLEM_WITH_REMITTANCE": "Remittance problem",
    "OTHER": "Other",
}
OPEN_STATUSES = {"OPEN", "WAITING_FOR_SELLER_RESPONSE", "WAITING_FOR_BUYER_RESPONSE", "UNDER_REVIEW", "OTHER"}
WON = {"RESOLVED_SELLER_FAVOUR", "CANCELED_BY_BUYER", "DENIED"}
LOST = {"RESOLVED_BUYER_FAVOUR", "RESOLVED_WITH_PAYOUT"}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class Service:
    def __init__(self) -> None:
        self.provider = PayPalClient() if settings.paypal_live else MockPayPal()
        self.running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._last_disputes: list[dict] = []
        self._ai_runs: list[float] = []  # monotonic timestamps, for the hourly AI budget
        self._ai_sem: asyncio.Semaphore | None = None  # created lazily inside the running event loop

    def _ai_budget_ok(self) -> bool:
        """Cap Claude runs per hour so a public demo URL can't run up the API bill."""
        now = time.monotonic()
        self._ai_runs = [t for t in self._ai_runs if now - t < 3600]
        if len(self._ai_runs) >= settings.max_ai_runs_per_hour:
            return False
        self._ai_runs.append(now)
        return True

    @property
    def mode(self) -> str:
        return self.provider.mode

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- read model ------------------------------------------------------------------------

    async def disputes(self) -> list[dict]:
        real_disputes = await self.provider.list_disputes()
        mock_disputes = store.state.get("mock_disputes", [])
        seen = set()
        combined = []
        for d in real_disputes + mock_disputes:
            did = d.get("dispute_id")
            if did and did not in seen:
                seen.add(did)
                combined.append(d)
        self._last_disputes = combined
        return self._last_disputes

    def row(self, d: dict) -> dict:
        tx = (d.get("disputed_transactions") or [{}])[0]
        amount = float((d.get("dispute_amount") or {}).get("value", 0))
        due = _parse(d.get("seller_response_due_date"))
        days_left = (due - utcnow()).total_seconds() / 86400 if due else None
        a = store.state["analyses"].get(d["dispute_id"])
        outcome = (d.get("dispute_outcome") or {}).get("outcome_code")
        is_open = d.get("status") in OPEN_STATUSES
        expected = None
        if a:
            if a["recommendation"] == "FIGHT":
                expected = round(amount * a["win_probability"] / 100, 2)
            elif a["recommendation"] == "OFFER":
                expected = round(max(amount - a.get("offer_amount", 0), 0), 2)
            else:
                expected = 0.0
        return {
            "dispute_id": d["dispute_id"],
            "buyer": (tx.get("buyer") or {}).get("name") or "-",
            "item": ", ".join(i.get("item_name", "") for i in tx.get("items", []) or []) or "-",
            "invoice": tx.get("invoice_number"),
            "reason": d.get("reason"),
            "reason_label": REASON_LABELS.get(d.get("reason"), d.get("reason")),
            "amount": amount,
            "currency": (d.get("dispute_amount") or {}).get("currency_code", "USD"),
            "status": d.get("status"),
            "stage": d.get("dispute_life_cycle_stage"),
            "created": d.get("create_time"),
            "due": d.get("seller_response_due_date"),
            "days_left": round(days_left, 2) if days_left is not None and is_open else None,
            "is_open": is_open,
            "needs_response": d.get("status") == "WAITING_FOR_SELLER_RESPONSE",
            "outcome": outcome,
            "recommendation": a["recommendation"] if a else None,
            "win_probability": a["win_probability"] if a else None,
            "expected_recovery": expected,
            "analyzing": d["dispute_id"] in self.running,
            "last_action": store.state["actions"].get(d["dispute_id"]),
            "offer": (d.get("offer") or {}).get("seller_offered_amount"),
        }

    def kpis(self, rows: list[dict]) -> dict:
        open_rows = [r for r in rows if r["is_open"]]
        resolved = [r for r in rows if not r["is_open"]]
        won = sum(1 for r in resolved if r["outcome"] in WON)
        lost = sum(1 for r in resolved if r["outcome"] in LOST)
        # Track record: the agent's win probability vs. PayPal's actual ruling. Settled disputes
        # (accepted / offer taken) have no ruling, so they're excluded.
        graded = [(r["win_probability"] / 100, 1.0 if r["outcome"] in WON else 0.0)
                  for r in resolved if r["win_probability"] is not None and r["outcome"] in WON | LOST]
        track = {
            "graded": len(graded),
            "correct": sum(1 for p, y in graded if (p >= 0.5) == (y == 1.0)),
            "brier": round(sum((p - y) ** 2 for p, y in graded) / len(graded), 3) if graded else None,
        }
        by_verdict: dict[str, float] = {"FIGHT": 0.0, "OFFER": 0.0, "ACCEPT": 0.0, "NONE": 0.0}
        for r in open_rows:
            by_verdict[r["recommendation"] or "NONE"] += r["amount"]
        return {
            "track": track,
            "impact": self.impact(rows),
            "at_risk_by_verdict": {k: round(v, 2) for k, v in by_verdict.items()},
            "open": len(open_rows),
            "needs_response": sum(1 for r in open_rows if r["needs_response"]),
            "at_risk": round(sum(r["amount"] for r in open_rows), 2),
            "due_soon": sum(1 for r in open_rows if r["needs_response"] and r["days_left"] is not None and r["days_left"] <= 3),
            "expected_recovery": round(sum(r["expected_recovery"] or 0 for r in open_rows), 2),
            "analyzed": sum(1 for r in open_rows if r["recommendation"]),
            "won": won,
            "lost": lost,
            "settled": sum(1 for r in resolved if r["outcome"] == "ACCEPTED"),
        }

    def impact(self, rows: list[dict]) -> dict:
        """What Dispute Defender did for the merchant. Money figures count only disputes it acted on;
        time saved is an estimate (MINUTES_PER_CASE per case the merchant didn't build by hand)."""
        actions = store.state["actions"]
        recovered = kept = avoided = 0.0
        avoided_n = 0
        for r in rows:
            act = actions.get(r["dispute_id"]) or {}
            if r["outcome"] in WON and act.get("kind") == "evidence":
                recovered += r["amount"]
            offer = (r.get("offer") or {})
            if r["outcome"] == "ACCEPTED" and act.get("kind") == "offer" and offer:
                kept += max(r["amount"] - float(offer.get("value", r["amount"])), 0)
            if act.get("kind") == "accept" and r["win_probability"] is not None and r["win_probability"] < 30:
                avoided += r["amount"]
                avoided_n += 1
        analyzed = len(store.state["analyses"])
        prevented = [v for v in store.state.get("radar_resolved", {}).values() if v.get("action") in ("reply", "hold", "reship")]
        return {
            "recovered": round(recovered, 2),
            "kept_by_offers": round(kept, 2),
            "losing_fights_avoided": avoided_n,
            "losing_fights_amount": round(avoided, 2),
            "hours_saved": round(analyzed * settings.minutes_per_case / 60, 1),
            "minutes_per_case": settings.minutes_per_case,
            "disputes_prevented": len(prevented),
            "prevented_amount": round(sum(v.get("amount", 0) for v in prevented), 2),
        }

    # ---- analysis --------------------------------------------------------------------------

    async def analyze(self, dispute_id: str) -> dict | None:
        if dispute_id in self.running:
            return None
        self.running.add(dispute_id)
        bus.publish("analysis_started", dispute_id=dispute_id)
        engine = "claude" if settings.ai_enabled else "rules"
        try:
            if not self._last_disputes:
                await self.disputes()
            ctx = ToolContext(provider=self.provider, disputes=self._last_disputes)
            emit = lambda step: bus.publish("agent_step", dispute_id=dispute_id, step=step)
            error = None
            if engine == "claude" and not self._ai_budget_ok():
                engine = "rules"
                error = f"Hourly AI budget of {settings.max_ai_runs_per_hour} investigations reached (MAX_AI_RUNS_PER_HOUR)"
            if engine == "claude":
                try:
                    # Webhooks, autopilot and "Analyze all" can overlap; keep free-tier providers under
                    # their requests-per-minute limit by bounding concurrent investigations.
                    if self._ai_sem is None:
                        self._ai_sem = asyncio.Semaphore(max(1, settings.ai_concurrency))
                    async with self._ai_sem:
                        analysis = await agent.investigate(dispute_id, ctx, emit)
                except Exception as exc:  # keep the merchant unblocked; record why AI analysis failed
                    error = f"{type(exc).__name__}: {exc}"
                    bus.publish("agent_error", dispute_id=dispute_id, error=error[:300])
                    analysis = await heuristic_investigate(dispute_id, ctx, emit)
            else:
                analysis = await heuristic_investigate(dispute_id, ctx, emit)
            if error:
                analysis["engine"]["error"] = error[:500]
            analysis["analyzed_at"] = iso(utcnow())
            store.state["analyses"][dispute_id] = analysis
            await store.log(
                f"Analysis complete: {analysis['recommendation']} ({analysis['win_probability']}% win chance) via {analysis['engine']['model']}",
                dispute_id,
            )
            bus.publish("analysis_done", dispute_id=dispute_id, recommendation=analysis["recommendation"], win_probability=analysis["win_probability"])
            self.spawn(self.alert(
                "analysis_done",
                f"🔍 {dispute_id}: agent recommends {analysis['recommendation']} ({analysis['win_probability']}% win chance). {analysis.get('summary', '')}",
                dispute_id=dispute_id, recommendation=analysis["recommendation"], win_probability=analysis["win_probability"],
            ))
            await self._autopilot(dispute_id, analysis)
            return analysis
        except Exception as exc:
            await store.log(f"Analysis failed: {exc}", dispute_id, "error")
            bus.publish("analysis_failed", dispute_id=dispute_id, error=str(exc)[:300])
            raise
        finally:
            self.running.discard(dispute_id)

    async def _autopilot(self, dispute_id: str, analysis: dict) -> None:
        cfg = store.state["settings"]
        if not cfg.get("autopilot") or analysis["recommendation"] != "FIGHT":
            return
        dispute = await self.provider.get_dispute(dispute_id, fresh=True)
        amount = float(dispute["dispute_amount"]["value"])
        if dispute.get("status") != "WAITING_FOR_SELLER_RESPONSE":
            return
        if analysis["win_probability"] < cfg["autopilot_min_win"] or amount > cfg["autopilot_max_amount"]:
            await store.log(
                f"Autopilot held for human review (win {analysis['win_probability']}% / ${amount:.2f} outside the "
                f"{cfg['autopilot_min_win']}% / ${cfg['autopilot_max_amount']:.0f} guardrails)",
                dispute_id,
            )
            return
        await self.submit_evidence(dispute_id, analysis["response_note"], actor="autopilot")

    def analysis(self, dispute_id: str) -> dict | None:
        return store.state["analyses"].get(dispute_id)

    # ---- actions (all require an explicit human click, except guarded autopilot) ------------

    async def _record(self, dispute_id: str, kind: str, detail: str, actor: str) -> None:
        store.state["actions"][dispute_id] = {"kind": kind, "detail": detail, "actor": actor, "time": iso(utcnow())}
        await store.log(f"{'Autopilot' if actor == 'autopilot' else 'Merchant'}: {detail}", dispute_id)
        bus.publish("dispute_updated", dispute_id=dispute_id)

    async def order_context(self, dispute: dict) -> tuple[dict | None, dict[str, dict]]:
        """The order behind a dispute (store database, else rebuilt from PayPal) plus carrier scans
        for its shipments (store tracking, else EasyPost)."""
        tx = (dispute.get("disputed_transactions") or [{}])[0]
        order = merchant_data.find_order(tx.get("seller_transaction_id", ""), tx.get("invoice_number", ""))
        if not order:
            order = await paypal_data.order_from_paypal(self.provider, tx.get("seller_transaction_id", ""))
        tracking: dict[str, dict] = {}
        for key in ("fulfillment", "return"):
            num = ((order or {}).get(key) or {}).get("tracking_number")
            if not num:
                continue
            t = merchant_data.get_tracking(num)
            if not t and easypost.enabled():
                try:
                    t = await easypost.track(num, (order[key] or {}).get("carrier") or "")
                except ValueError:
                    t = None
            if t:
                tracking[num] = t
        return order, tracking

    async def evidence_pdf(self, dispute_id: str, note: str | None = None) -> bytes:
        analysis = self.analysis(dispute_id) or {}
        dispute = await self.provider.get_dispute(dispute_id)
        order, tracking = await self.order_context(dispute)
        return build_pdf(dispute, analysis, note or analysis.get("response_note", ""), settings.merchant_name, order, tracking)

    async def submit_evidence(self, dispute_id: str, note: str, actor: str = "merchant") -> dict:
        analysis = self.analysis(dispute_id)
        if not analysis:
            raise ValueError("Run the analysis before submitting evidence.")
        dispute = await self.provider.get_dispute(dispute_id, fresh=True)
        pdf_name = f"evidence-{dispute_id}.pdf"
        order, tracking = await self.order_context(dispute)
        pdf = build_pdf(dispute, analysis, note, settings.merchant_name, order, tracking)
        evidences = build_evidences(analysis, note, pdf_name)
        result = await self.provider.provide_evidence(dispute_id, evidences, pdf, pdf_name)
        types = ", ".join(sorted({e["evidence_type"] for e in evidences}))
        await self._record(dispute_id, "evidence", f"Submitted evidence to PayPal ({types}) via {result['channel']}", actor)
        return result

    async def accept(self, dispute_id: str, note: str) -> dict:
        result = await self.provider.accept_claim(dispute_id, note)
        await self._record(dispute_id, "accept", "Accepted the claim and refunded the buyer", "merchant")
        return result

    async def offer(self, dispute_id: str, note: str, offer_type: str, amount: float) -> dict:
        dispute = await self.provider.get_dispute(dispute_id)
        currency = dispute["dispute_amount"]["currency_code"]
        result = await self.provider.make_offer(dispute_id, note, offer_type, amount, currency)
        label = f"${amount:.2f} {offer_type.replace('_', ' ').lower()}" if amount else offer_type.replace("_", " ").lower()
        await self._record(dispute_id, "offer", f"Offered the buyer a {label}", "merchant")
        if self.mode == "mock":
            self.spawn(self._mock_buyer_accepts(dispute_id))
        return result

    async def _mock_buyer_accepts(self, dispute_id: str) -> None:
        await asyncio.sleep(8)
        await self.provider.buyer_accepts_offer(dispute_id)
        await store.log("Buyer accepted the offer - dispute closed", dispute_id)
        bus.publish("dispute_updated", dispute_id=dispute_id, toast="Buyer accepted your offer - dispute closed")

    async def message(self, dispute_id: str, text: str) -> dict:
        result = await self.provider.send_message(dispute_id, text)
        await self._record(dispute_id, "message", "Sent a message to the buyer", "merchant")
        return result

    async def simulate_outcome(self, dispute_id: str, outcome: str) -> dict:
        result = await self.provider.simulate_outcome(dispute_id, outcome)
        who = "seller" if outcome == "SELLER_FAVOR" else "buyer"
        await store.log(f"PayPal decided the dispute in the {who}'s favour (sandbox simulation)", dispute_id)
        bus.publish("dispute_updated", dispute_id=dispute_id, toast=f"PayPal ruled in the {who}'s favour")
        return result

    # ---- webhooks & simulations -------------------------------------------------------------

    async def handle_webhook(self, event: dict) -> None:
        event_type = event.get("event_type", "")
        resource = event.get("resource") or {}
        dispute_id = resource.get("dispute_id")
        if not event_type.startswith("CUSTOMER.DISPUTE.") or not dispute_id:
            return
        await store.log(f"Webhook {event_type}", dispute_id)
        if event_type == "CUSTOMER.DISPUTE.CREATED":
            bus.publish("dispute_created", dispute_id=dispute_id, toast=f"New PayPal dispute {dispute_id} - agent investigating")
            self.spawn(self.alert("dispute_created", f"🆕 New PayPal dispute {dispute_id}. Dispute Defender is investigating.", dispute_id=dispute_id))
            self.spawn(self._fresh_then_analyze(dispute_id))
        else:
            bus.publish("dispute_updated", dispute_id=dispute_id)

    async def _fresh_then_analyze(self, dispute_id: str) -> None:
        await self.disputes()
        try:
            await self.analyze(dispute_id)
        except Exception:
            pass  # already logged and broadcast by analyze()

    async def simulate_new_dispute(self) -> dict:
        if self.mode == "mock":
            dispute = await self.provider.simulate_new_dispute()
        else:
            mock_prov = MockPayPal()
            dispute = await mock_prov.simulate_new_dispute()
        # Exercise the same path a real PayPal webhook would take.
        await self.handle_webhook({"event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {"dispute_id": dispute["dispute_id"]}})
        return dispute

    # ---- alerts (Slack / Zapier / any webhook) ---------------------------------------------

    async def alert(self, event: str, text: str, **data) -> None:
        """POST {"text", "event", ...} to ALERT_WEBHOOK_URL (Slack incoming webhooks read `text`;
        Zapier catch hooks take any JSON). Failures are logged, never raised."""
        if not settings.alert_webhook_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(settings.alert_webhook_url, json={"text": text, "event": event, "merchant": settings.merchant_name, **data})
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            await store.log(f"Alert webhook failed: {exc}", data.get("dispute_id"), "warn")

    async def check_deadlines(self) -> list[str]:
        """Alert once per dispute when a response is due within 48 hours."""
        alerted = store.state.setdefault("deadline_alerted", [])
        fired = []
        for d in await self.disputes():
            r = self.row(d)
            if r["needs_response"] and r["days_left"] is not None and r["days_left"] <= 2 and r["dispute_id"] not in alerted:
                alerted.append(r["dispute_id"])
                fired.append(r["dispute_id"])
                verdict = f" Agent recommends {r['recommendation']} ({r['win_probability']}%)." if r["recommendation"] else ""
                await self.alert("deadline", f"⏰ PayPal dispute {r['dispute_id']} ({r['reason_label']}, ${r['amount']:.2f}) needs a response within {r['days_left'] * 24:.0f}h.{verdict}", dispute_id=r["dispute_id"])
                bus.publish("deadline", dispute_id=r["dispute_id"], hours=round(r["days_left"] * 24))
        if fired:
            await store.save()
        return fired

    async def deadline_watcher(self) -> None:
        while True:
            try:
                await self.check_deadlines()
            except Exception as exc:  # keep the watcher alive through transient PayPal errors
                await store.log(f"Deadline check failed: {exc}", level="warn")
            await asyncio.sleep(1800)

    # ---- Dispute Radar (prevention) ---------------------------------------------------------

    async def radar(self) -> list[dict]:
        return radar.scan(await self.disputes())

    async def _radar_item(self, item_id: str) -> dict:
        item = next((i for i in await self.radar() if i["id"] == item_id), None)
        if not item:
            raise KeyError(item_id)
        return item

    async def radar_draft(self, item_id: str) -> dict:
        item = await self._radar_item(item_id)
        if settings.ai_enabled and self._ai_budget_ok():
            try:
                return await agent.draft_radar_reply(radar.context_for(item))
            except Exception as exc:
                draft = radar.template_reply(item)
                draft["engine"]["error"] = str(exc)[:200]
                return draft
        return radar.template_reply(item)

    async def radar_act(self, item_id: str, action: str, reply: str = "") -> dict:
        item = await self._radar_item(item_id)
        if action == "reply" and not reply.strip():
            raise ValueError("Write a reply first.")
        summary = await radar.resolve(item, action, reply)
        await store.log(f"Radar: {summary}")
        bus.publish("radar_updated", toast=summary)
        return {"ok": True, "summary": summary}

    # ---- assistant features: what-if chat, translation, carrier claims ----------------------

    def _need_ai(self) -> None:
        if not settings.ai_enabled:
            raise ValueError("This feature needs an AI key: set GEMINI_API_KEY (free) or ANTHROPIC_API_KEY.")
        if not self._ai_budget_ok():
            raise ValueError(f"Hourly AI budget of {settings.max_ai_runs_per_hour} reached; try again later.")

    async def ask(self, dispute_id: str, question: str, history: list[dict]) -> dict:
        self._need_ai()
        dispute = await self.provider.get_dispute(dispute_id)
        order, tracking = await self.order_context(dispute)
        analysis = {k: v for k, v in (self.analysis(dispute_id) or {}).items() if k not in ("trace", "engine")}
        context = {"dispute": summarize_dispute(dispute), "analysis": analysis or None, "order": order, "tracking": tracking}
        return await agent.ask_about_dispute(context, question, history)

    async def translate(self, text: str, target: str) -> dict:
        self._need_ai()
        return await agent.translate(text, target)

    async def carrier_claim(self, dispute_id: str) -> dict:
        dispute = await self.provider.get_dispute(dispute_id)
        order, tracking = await self.order_context(dispute)
        if not order or not order.get("fulfillment"):
            raise ValueError("No shipment on record for this dispute, so there's nothing to claim from a carrier.")
        analysis = self.analysis(dispute_id) or {}
        f = order["fulfillment"]
        track = tracking.get(f.get("tracking_number")) or {}
        photos = [a for t in merchant_data.search_support(order["customer"]["email"]) for a in t.get("attachments", []) or []]
        context = {
            "merchant": settings.merchant_name,
            "order": order,
            "tracking": track,
            "buyer_photos": photos,
            "dispute": summarize_dispute(dispute),
            "analysis_key_facts": analysis.get("key_facts"),
            "analysis_summary": analysis.get("summary"),
        }
        if settings.ai_enabled and self._ai_budget_ok():
            try:
                return await agent.draft_carrier_claim(context)
            except Exception:
                pass  # fall through to the template
        damaged = any("damage" in e.get("description", "").lower() for e in track.get("events", []))
        delivered = track.get("status") == "DELIVERED"
        kind = "DAMAGE" if damaged or (delivered and dispute.get("reason") == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED") else "LOSS" if not delivered else "NOT_APPLICABLE"
        items = "; ".join(f"{i['qty']} x {i['name']} ({i['price']})" for i in order["items"])
        letter = (
            f"To {f.get('carrier')} claims,\n\nShipper: {settings.merchant_name}\nTracking number: {f.get('tracking_number')}\n"
            f"Ship date: {(f.get('shipped') or '')[:10]}\nRecipient: {order['shipping_address'].get('name')}, {order['shipping_address'].get('city')}, {order['shipping_address'].get('state')}\n"
            f"Contents: {items}\nDeclared value: {order['total']} {order.get('currency', 'USD')}\n\n"
            + ("The parcel was delivered damaged. Your tracking shows: " + "; ".join(e["description"] for e in track.get("events", []) if "damage" in e["description"].lower())
               + f". The recipient documented the damage with {len(photos)} photo(s) on delivery." if kind == "DAMAGE" else
               "The parcel has not been delivered and tracking shows no recent movement." if kind == "LOSS" else
               "Tracking shows normal delivery; a carrier claim may not apply.")
            + "\n\nPlease open a claim for the value above. Photos and the invoice are attached.\n"
        )
        return {
            "carrier": f.get("carrier"),
            "claim_type": kind,
            "claim_amount": float(order["total"]),
            "letter": letter,
            "checklist": ["Invoice / proof of value", "Tracking number and shipping label", "Photos of the damaged item and packaging" if kind == "DAMAGE" else "Proof the item wasn't received", "Keep the packaging until the claim is settled"],
            "deadline_note": "Carriers set their own filing windows - check the carrier's claims page and file promptly.",
            "engine": {"kind": "rules"},
        }

    # ---- insights --------------------------------------------------------------------------

    async def insights(self) -> dict:
        disputes = await self.disputes()
        rows = []
        for d in disputes:
            r = self.row(d)
            a = self.analysis(d["dispute_id"]) or {}
            rows.append({
                **{k: r[k] for k in ("dispute_id", "item", "reason_label", "amount", "status", "outcome", "days_left")},
                "recommendation": a.get("recommendation"),
                "win_probability": a.get("win_probability"),
                "summary": a.get("summary"),
                "process_fix": a.get("process_fix"),
                "key_facts": [f["fact"] for f in a.get("key_facts", [])][:6],
            })
        if settings.ai_enabled:
            return await agent.portfolio_insights(rows)
        by_reason: dict[str, list] = {}
        for r in rows:
            by_reason.setdefault(r["reason_label"], []).append(r)
        insights = [
            {
                "title": f"{label}: {len(items)} dispute(s), ${sum(i['amount'] for i in items):.2f}",
                "detail": " ".join(sorted({i["process_fix"].rstrip(".") + "." for i in items if i.get("process_fix")})) or "No process fix suggested yet - analyze these disputes first.",
                "action": "Review the suggested process fixes for this category.",
                "impact": "HIGH" if len(items) > 1 else "MEDIUM",
            }
            for label, items in sorted(by_reason.items(), key=lambda kv: -sum(i["amount"] for i in kv[1]))
        ]
        return {"headline": "Rule-based summary (add an AI key, e.g. a free GEMINI_API_KEY, for AI root-cause analysis)", "insights": insights[:5], "engine": {"kind": "rules"}}

    # ---- demo storefront (sandbox mode) ----------------------------------------------------

    async def store_create_order(self, sku: str) -> dict:
        product = next((p for p in store.merchant["products"] if p["sku"] == sku), None)
        if not product:
            raise ValueError("Unknown product")
        order_id = f"KC-{3000 + len(store.merchant['orders'])}-{''.join(random.choices(string.digits, k=3))}"
        shipping = "0.00" if sku == "KC-PLANTER" else "8.00"
        result = await self.provider.create_order(order_id, product, shipping)
        store.state.setdefault("pending_orders", {})[result["id"]] = {"order_id": order_id, "sku": sku, "shipping": shipping}
        await store.save()
        return {"id": result["id"]}

    async def store_capture(self, paypal_order_id: str) -> dict:
        pending = store.state.get("pending_orders", {}).pop(paypal_order_id, None)
        if not pending:
            raise ValueError("Unknown PayPal order")
        cap = await self.provider.capture_order(paypal_order_id)
        unit = cap["purchase_units"][0]
        capture = unit["payments"]["captures"][0]
        payer = cap.get("payer", {})
        ship = unit.get("shipping", {})
        addr = ship.get("address", {})
        product = next(p for p in store.merchant["products"] if p["sku"] == pending["sku"])
        name = " ".join(filter(None, [payer.get("name", {}).get("given_name"), payer.get("name", {}).get("surname")]))
        now = utcnow()
        # With an EasyPost *test* key, EZ4000000004 is EasyPost's documented "delivered" test code, so the
        # agent gets carrier scans from a real API. Otherwise we simulate the carrier locally.
        use_easypost = easypost.enabled()
        tracking_number = "EZ4000000004" if use_easypost else "9400" + "".join(random.choices(string.digits, k=18))
        order = {
            "order_id": pending["order_id"],
            "invoice_id": pending["order_id"],
            "paypal_order_id": paypal_order_id,
            "paypal_capture_id": capture["id"],
            "placed": iso(now),
            "customer": {"name": name or "Sandbox Buyer", "email": payer.get("email_address", "")},
            "items": [{"sku": product["sku"], "name": product["title"], "qty": 1, "price": product["price"]}],
            "shipping_cost": pending["shipping"],
            "total": capture["amount"]["value"],
            "currency": capture["amount"]["currency_code"],
            "shipping_address": {
                "name": (ship.get("name") or {}).get("full_name", name),
                "line1": addr.get("address_line_1", ""),
                "city": addr.get("admin_area_2", ""),
                "state": addr.get("admin_area_1", ""),
                "postal_code": addr.get("postal_code", ""),
                "country_code": addr.get("country_code", ""),
            },
            "ip": "sandbox", "device_id": "sandbox-browser",
            "fulfillment": {"carrier": "USPS", "tracking_number": tracking_number, "shipped": iso(now)},
            "refunds": [],
            "internal_notes": "Placed through the Dispute Defender sandbox storefront. "
            + ("Carrier scans come from EasyPost test mode." if use_easypost else "Carrier scans are simulated."),
        }
        store.merchant["orders"].append(order)
        if not use_easypost:
            # Simulated carrier: delivered to the PayPal ship-to address, so INR disputes have evidence to find.
            city = f"{order['shipping_address']['city']}, {order['shipping_address']['state']}".strip(", ")
            store.merchant["tracking"][tracking_number] = {
                "carrier": "USPS", "status": "DELIVERED", "signed_by": "",
                "events": [
                    {"time": iso(now + timedelta(minutes=5)), "location": "Portland, OR", "description": "Accepted at USPS Origin Facility"},
                    {"time": iso(now + timedelta(minutes=30)), "location": city, "description": f"Delivered, Front Door/Porch ({order['shipping_address']['line1']})"},
                ],
            }
        await store.save()
        tracking_status = "added"
        try:
            await self.provider.add_tracking(paypal_order_id, capture["id"], tracking_number, "USPS")
        except Exception as exc:
            tracking_status = f"failed: {exc}"
        await store.log(f"Storefront sale {order['order_id']} captured ({capture['id']}); tracking {tracking_status}")
        return {"order": order, "capture_id": capture["id"], "tracking": tracking_status}


service = Service()
