"""JSON-file persistence for analyses, activity, settings, demo-store orders and the mock PayPal state.

A single JSON document keeps the project dependency-free and easy for judges to inspect
(`data/state.json`). Writes are atomic (temp file + rename) and serialised with an asyncio lock.
"""

import asyncio
import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, settings

SEED_PATH = ROOT / "data" / "seed.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.state: dict = {}

    def load(self) -> None:
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = self.fresh_state()
            self._write()

    @staticmethod
    def fresh_state() -> dict:
        from .seed import build_seed  # local import: seed builds relative dates at call time

        seed = build_seed(json.loads(SEED_PATH.read_text(encoding="utf-8")))
        return {
            "version": 1,
            "settings": {"autopilot": False, "autopilot_min_win": 80, "autopilot_max_amount": 150.0},
            "analyses": {},
            "actions": {},
            "activity": [],
            "merchant": seed["merchant"],
            "mock_disputes": seed["mock_disputes"],
            "mock_transactions": seed["mock_transactions"],
            "simulations": seed["simulations"],
            "mock_counter": 0,
        }

    async def reset(self) -> None:
        async with self._lock:
            self.state = self.fresh_state()
            self._write()

    async def save(self) -> None:
        async with self._lock:
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- convenience accessors -------------------------------------------------------------

    @property
    def merchant(self) -> dict:
        return self.state["merchant"]

    def snapshot(self, key: str):
        return copy.deepcopy(self.state.get(key))

    async def log(self, message: str, dispute_id: str | None = None, level: str = "info") -> dict:
        entry = {"time": iso(utcnow()), "message": message, "dispute_id": dispute_id, "level": level}
        self.state["activity"].insert(0, entry)
        del self.state["activity"][200:]
        await self.save()
        return entry


store = Store(settings.data_dir / "state.json")
