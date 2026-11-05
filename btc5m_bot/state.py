"""Thread-safe snapshot store shared by the engine and the dashboard API."""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


def _default_state() -> dict[str, Any]:
    return {
        "app": "POLYBOT",
        "started_at": None,
        "uptime_seconds": 0,
        "mode": "PAPER",
        "effective_mode": "PAPER",
        "live_locked": True,
        "engine_healthy": False,
        "detail": "starting",
        "recovery": {"state": "STARTING", "blocks": [], "notes": []},
        "risk": None,
        "feeds": [],
        "oracle": {"health": "UNVERIFIED", "detail": "not started"},
        "market": None,
        "books": {},
        "features": None,
        "probability": None,
        "regime": None,
        "position": None,
        "last_decision": None,
        "counters": {"decisions": 0, "entries": 0, "exits": 0, "settlements": 0, "errors": 0},
        "latency": {},
        "live": {"activation_active": False, "heartbeat": {}, "gate": None},
        "config": {},
        "analytics": {},
        "updated_at": None,
    }


class EngineStateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = _default_state()

    def reset(self) -> None:
        with self._lock:
            self._state = _default_state()

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                self._state[key] = value
            self._state["updated_at"] = datetime.now(timezone.utc).isoformat()

    def increment(self, key: str, amount: int = 1) -> int:
        with self._lock:
            counters = dict(self._state.get("counters") or {})
            counters[key] = int(counters.get(key, 0)) + amount
            self._state["counters"] = counters
            return counters[key]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)
