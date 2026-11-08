"""Carrier tracking through EasyPost (optional; set EASYPOST_API_KEY).

PayPal stores tracking numbers but not carrier scan events, and scan events (delivered, signed by)
are what win "item not received" disputes. EasyPost's official API returns them for USPS, UPS,
FedEx, DHL and more. With a *test* key, the documented test codes return fixed statuses, e.g.
EZ4000000004 -> delivered, EZ2000000002 -> in_transit - perfect for a sandbox demo.
"""

import time

import httpx

from .config import settings

API = "https://api.easypost.com/v2"
STATUS = {
    "delivered": "DELIVERED", "in_transit": "IN_TRANSIT", "out_for_delivery": "OUT_FOR_DELIVERY",
    "pre_transit": "PRE_TRANSIT", "available_for_pickup": "AVAILABLE_FOR_PICKUP", "return_to_sender": "RETURN_TO_SENDER",
    "failure": "FAILURE", "cancelled": "CANCELLED", "error": "ERROR", "unknown": "UNKNOWN",
}

_cache: dict[str, tuple[float, dict]] = {}
_http: httpx.AsyncClient | None = None


def enabled() -> bool:
    return bool(settings.easypost_api_key)


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(base_url=API, auth=(settings.easypost_api_key, ""), timeout=30)
    return _http


async def close() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


async def track(tracking_number: str, carrier: str = "") -> dict | None:
    """Create (or refresh) an EasyPost tracker and normalise it to our tracking shape."""
    if not enabled() or not tracking_number:
        return None
    cached = _cache.get(tracking_number)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    body = {"tracker": {"tracking_code": tracking_number}}
    if carrier:
        body["tracker"]["carrier"] = carrier
    resp = await _client().post("/trackers", json=body)
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {}).get("message")
        except ValueError:
            err = resp.text[:200]
        raise ValueError(f"EasyPost tracking failed ({resp.status_code}): {err}")
    t = resp.json()
    events = []
    for d in t.get("tracking_details") or []:
        loc = d.get("tracking_location") or {}
        events.append({
            "time": d.get("datetime"),
            "location": ", ".join(filter(None, [loc.get("city"), loc.get("state"), loc.get("zip")])) or "-",
            "description": d.get("message") or d.get("status") or "",
        })
    result = {
        "tracking_number": tracking_number,
        "carrier": t.get("carrier") or carrier,
        "status": STATUS.get(t.get("status") or "unknown", (t.get("status") or "UNKNOWN").upper()),
        "signed_by": t.get("signed_by") or "",
        "events": events,
        "public_url": t.get("public_url"),
        "source": f"EasyPost ({t.get('mode', 'unknown')} mode)",
    }
    _cache[tracking_number] = (time.monotonic(), result)
    return result
