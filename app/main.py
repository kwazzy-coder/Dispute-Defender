"""HTTP API + static dashboard for Dispute Defender."""

import asyncio
import hmac
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, easypost, merchant_data
from .config import ROOT, settings
from .events import bus
from .paypal import PayPalError
from .service import service
from .store import store

STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.load()
    watcher = asyncio.create_task(service.deadline_watcher())
    yield
    watcher.cancel()
    await service.provider.close()
    await agent.close_client()
    await easypost.close()


app = FastAPI(title="Dispute Defender", version="1.1.0", lifespan=lifespan)


def require_operator(x_demo_password: str | None = Header(default=None)) -> None:
    """When DEMO_PASSWORD is set, actions that change PayPal or app state need it.

    Browsing and AI analysis stay open (analysis is capped by MAX_AI_RUNS_PER_HOUR), so judges can
    explore a public demo URL without being able to submit, accept or reset anything."""
    if not settings.demo_password:
        return
    if not x_demo_password or not hmac.compare_digest(x_demo_password.encode(), settings.demo_password.encode()):
        raise HTTPException(401, "This action needs the demo password.")


operator = [Depends(require_operator)]
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(PayPalError)
async def paypal_error(_: Request, exc: PayPalError):
    return JSONResponse({"error": str(exc)}, status_code=502)


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def key_error(_: Request, exc: KeyError):
    return JSONResponse({"error": f"Not found: {exc}"}, status_code=404)


class NoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class OfferBody(NoteBody):
    offer_type: str = Field(pattern="^(REFUND|REFUND_WITH_RETURN|REFUND_WITH_REPLACEMENT|REPLACEMENT_WITHOUT_REFUND)$")
    amount: float = Field(ge=0)


class MessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class OutcomeBody(BaseModel):
    outcome: str = Field(pattern="^(SELLER_FAVOR|BUYER_FAVOR)$")


class SettingsBody(BaseModel):
    autopilot: bool
    autopilot_min_win: int = Field(ge=50, le=100)
    autopilot_max_amount: float = Field(ge=0, le=10000)


class StoreOrderBody(BaseModel):
    sku: str


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=600)
    history: list[dict] = Field(default_factory=list, max_length=20)


class TranslateBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    target: str = Field(default="English", max_length=40)


class RadarActionBody(BaseModel):
    action: str = Field(pattern="^(reply|hold|reship|dismiss)$")
    reply: str = Field(default="", max_length=3000)


# ---- pages ---------------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/store", include_in_schema=False)
async def storefront():
    return FileResponse(STATIC / "store.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mode": service.mode}


# ---- dashboard API -------------------------------------------------------------------------


@app.get("/api/config")
async def config():
    return {
        "mode": service.mode,
        "ai_enabled": settings.ai_enabled,
        "model": settings.model_label if settings.ai_enabled else "rule-based fallback",
        "ai_provider": settings.ai_provider,
        "merchant": settings.merchant_name,
        "paypal_client_id": settings.paypal_client_id if service.mode == "sandbox" else "",
        "webhook_configured": bool(settings.paypal_webhook_id),
        "auth_required": bool(settings.demo_password),
        "easypost_enabled": easypost.enabled(),
        "alerts_webhook": bool(settings.alert_webhook_url),
        "settings": store.state["settings"],
    }


@app.post("/api/auth/check", dependencies=operator)
async def auth_check():
    return {"ok": True}


@app.get("/api/attachments/{name}")
async def attachment(name: str):
    loaded = merchant_data.load_attachment(name)
    if not loaded:
        raise HTTPException(404, "No such attachment")
    data, media = loaded
    return Response(data, media_type=media, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/disputes")
async def list_disputes():
    disputes = await service.disputes()
    rows = [service.row(d) for d in disputes]
    rows.sort(key=lambda r: (not r["needs_response"], not r["is_open"], r["days_left"] if r["days_left"] is not None else 1e9))
    return {"rows": rows, "kpis": service.kpis(rows)}


@app.get("/api/disputes/{dispute_id}")
async def get_dispute(dispute_id: str):
    dispute = await service.provider.get_dispute(dispute_id, fresh=True)
    tx = (dispute.get("disputed_transactions") or [{}])[0]
    order = merchant_data.find_order(tx.get("seller_transaction_id", ""), tx.get("invoice_number", ""))
    photos = []
    if order:
        for ticket in merchant_data.search_support(order["customer"]["email"]):
            for name in ticket.get("attachments", []) or []:
                photos.append({"name": name, "url": f"/api/attachments/{name}", "ticket": ticket["id"], "time": ticket.get("time")})
    return {"dispute": dispute, "row": service.row(dispute), "analysis": service.analysis(dispute_id), "photos": photos}


@app.post("/api/disputes/{dispute_id}/analyze", status_code=202)
async def analyze(dispute_id: str):
    if dispute_id in service.running:
        return {"started": False, "reason": "already running"}
    service.spawn(_safe_analyze(dispute_id))
    return {"started": True}


async def _safe_analyze(dispute_id: str):
    try:
        await service.analyze(dispute_id)
    except Exception:
        pass  # logged + broadcast inside service.analyze


@app.post("/api/analyze-all", status_code=202)
async def analyze_all():
    disputes = await service.disputes()
    todo = [
        d["dispute_id"]
        for d in disputes
        if d.get("status") == "WAITING_FOR_SELLER_RESPONSE" and not service.analysis(d["dispute_id"])
    ]

    async def run():
        sem = asyncio.Semaphore(max(1, settings.ai_concurrency))

        async def one(did):
            async with sem:
                await _safe_analyze(did)

        await asyncio.gather(*(one(d) for d in todo))

    service.spawn(run())
    return {"queued": todo}


@app.post("/api/disputes/{dispute_id}/evidence", dependencies=operator)
async def submit_evidence(dispute_id: str, body: NoteBody):
    return await service.submit_evidence(dispute_id, body.note)


@app.post("/api/disputes/{dispute_id}/accept", dependencies=operator)
async def accept(dispute_id: str, body: NoteBody):
    return await service.accept(dispute_id, body.note)


@app.post("/api/disputes/{dispute_id}/offer", dependencies=operator)
async def offer(dispute_id: str, body: OfferBody):
    return await service.offer(dispute_id, body.note, body.offer_type, body.amount)


@app.post("/api/disputes/{dispute_id}/message", dependencies=operator)
async def message(dispute_id: str, body: MessageBody):
    return await service.message(dispute_id, body.message)


@app.post("/api/disputes/{dispute_id}/simulate-outcome", dependencies=operator)
async def simulate_outcome(dispute_id: str, body: OutcomeBody):
    return await service.simulate_outcome(dispute_id, body.outcome)


@app.get("/api/disputes/{dispute_id}/evidence.pdf")
async def evidence_pdf(dispute_id: str):
    pdf = await service.evidence_pdf(dispute_id)
    headers = {"Content-Disposition": f'inline; filename="evidence-{dispute_id}.pdf"'}
    return Response(pdf, media_type="application/pdf", headers=headers)


@app.post("/api/disputes/{dispute_id}/ask")
async def ask(dispute_id: str, body: AskBody):
    history = [{"role": str(h.get("role", ""))[:10], "content": str(h.get("content", ""))[:2000]} for h in body.history]
    return await service.ask(dispute_id, body.question, history)


@app.post("/api/disputes/{dispute_id}/carrier-claim")
async def carrier_claim(dispute_id: str):
    return await service.carrier_claim(dispute_id)


@app.post("/api/translate")
async def translate(body: TranslateBody):
    return await service.translate(body.text, body.target)


@app.get("/api/radar")
async def radar_list():
    return {"items": await service.radar()}


@app.post("/api/radar/{item_id}/draft")
async def radar_draft(item_id: str):
    return await service.radar_draft(item_id)


@app.post("/api/radar/{item_id}/action", dependencies=operator)
async def radar_action(item_id: str, body: RadarActionBody):
    return await service.radar_act(item_id, body.action, body.reply)


@app.post("/api/simulate-dispute")
async def simulate_dispute():
    dispute = await service.simulate_new_dispute()
    return {"dispute_id": dispute["dispute_id"]}


@app.post("/api/insights")
async def insights():
    return await service.insights()


@app.get("/api/settings")
async def get_settings():
    return store.state["settings"]


@app.put("/api/settings", dependencies=operator)
async def put_settings(body: SettingsBody):
    store.state["settings"] = body.model_dump()
    await store.log(
        f"Autopilot {'ON' if body.autopilot else 'OFF'} (auto-submit when win >= {body.autopilot_min_win}% and amount <= ${body.autopilot_max_amount:.0f})"
    )
    bus.publish("settings_updated")
    return store.state["settings"]


@app.get("/api/activity")
async def activity():
    return store.state["activity"][:50]


@app.post("/api/reset", dependencies=operator)
async def reset():
    if service.mode != "mock":
        raise HTTPException(400, "Reset is only available in demo mode")
    await store.reset()
    bus.publish("reset")
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request):
    queue = bus.subscribe()

    async def stream():
        try:
            yield bus.encode({"kind": "hello", "mode": service.mode})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield bus.encode(event)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- PayPal webhooks -----------------------------------------------------------------------


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    headers = {k.lower(): v for k, v in request.headers.items()}
    if service.mode == "sandbox":
        try:
            verified = await service.provider.verify_webhook(headers, raw)
        except PayPalError:
            verified = False
        if not verified:
            await store.log(f"Rejected unverified webhook {event.get('event_type')}", level="warn")
            raise HTTPException(400, "Webhook signature verification failed")
    await service.handle_webhook(event)
    return {"ok": True}


# ---- demo storefront (creates real sandbox payments to dispute) ----------------------------


@app.get("/api/store/products")
async def store_products():
    return {"products": store.merchant["products"], "mode": service.mode, "client_id": settings.paypal_client_id if service.mode == "sandbox" else ""}


@app.post("/api/store/orders")
async def store_create(body: StoreOrderBody):
    if service.mode != "sandbox":
        raise HTTPException(400, "The storefront needs PayPal sandbox credentials (see README).")
    return await service.store_create_order(body.sku)


@app.post("/api/store/orders/{paypal_order_id}/capture")
async def store_capture(paypal_order_id: str):
    if service.mode != "sandbox":
        raise HTTPException(400, "The storefront needs PayPal sandbox credentials (see README).")
    return await service.store_capture(paypal_order_id)
