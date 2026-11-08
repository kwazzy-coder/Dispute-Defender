"""Turns the relative-date seed file into concrete timestamps so the demo always looks "live"."""

import copy
from datetime import datetime, timedelta, timezone


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def resolve_dates(obj, now: datetime | None = None, replacements: dict | None = None):
    """Recursively rewrite `foo_days_ago: n` -> `foo: <iso>` and `foo_in_days: n` -> `foo: <iso>`.

    A bare `days_ago` key becomes `time`. String values may contain `{placeholders}` filled from
    `replacements` (used for simulated disputes that need fresh PayPal-style IDs).
    """
    now = now or datetime.now(timezone.utc)
    replacements = replacements or {}

    def walk(value):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "days_ago":
                    out["time"] = _iso(now - timedelta(days=float(item)))
                elif key.endswith("_days_ago"):
                    out[key[: -len("_days_ago")]] = _iso(now - timedelta(days=float(item)))
                elif key.endswith("_in_days"):
                    out[key[: -len("_in_days")]] = _iso(now + timedelta(days=float(item)))
                else:
                    out[key] = walk(item)
            return out
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str) and replacements and "{" in value:
            for placeholder, replacement in replacements.items():
                value = value.replace("{" + placeholder + "}", replacement)
            return value
        return value

    return walk(copy.deepcopy(obj))


def build_seed(raw: dict) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "merchant": resolve_dates(raw["merchant"], now),
        "mock_disputes": resolve_dates(raw["mock_disputes"], now),
        "mock_transactions": resolve_dates(raw["mock_transactions"], now),
        "simulations": raw["simulations"],  # resolved lazily, at simulation time
    }
