"""FastAPI application for the loopback-only operator dashboard."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..analytics.calibration import calibration_report
from ..analytics.performance import compute_performance, daily_performance, loss_breakdown
from ..analytics.validation import _calibration_predictions, build_validation_report
from ..config import ConfigStore, HardLimitError, Settings
from ..security import redact
from .auth import CSRF_HEADER, SESSION_COOKIE, AuthError, AuthManager, Session

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# A Host header is only echoed into the CSP when it is a plain host[:port].
_HOST_RE = re.compile(r"[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?")

CONFIG_WRITABLE = {
    "trading_enabled",
    "capital_usd",
    "capital_from_balance",
    "risk_per_trade_pct",
    "trade_size_usd",
    "daily_loss_limit_usd",
    "daily_profit_lock_pct",
    "max_consecutive_losses",
    "cooldown_minutes",
    "entry_window_seconds",
    "min_probability",
    "min_net_edge",
    "max_spread",
    "max_slippage",
    "min_depth_multiple",
    "take_profit_roi",
    "dynamic_take_profit",
    "dynamic_tp_min_multiple",
    "dynamic_tp_max_multiple",
    "stop_loss_roi",
    "trailing_stop_pct",
    "momentum_reversal_z",
    "min_hold_seconds_before_reversal",
    "liquidity_exit_depth_drop_pct",
    "liquidity_exit_spread_multiple",
    "time_exit_seconds_before_end",
    "min_oracle_coverage_seconds",
    "require_rules_approval",
    "strategy",
    "strategy_mode",
    "feature_modes",
    "max_data_age_ms",
    "max_book_age_ms",
    "max_ack_latency_ms",
    "max_latency_p95_ms",
    "taker_delay_ms",
    "fee_buffer",
    "auto_approve_rules",
    "min_order_notional_usd",
    "paper_validation_min_trades",
}

PHRASES = {
    "kill": "KILL",
    "clear_kill": "CLEAR KILL",
    "reset_day": "RESET DAY",
    "approve_rule": "APPROVE RULE",
    "revoke_live": "REVOKE LIVE",
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _row(row: Any) -> dict[str, Any]:
    return {col.key: _jsonable(getattr(row, col.key)) for col in row.__table__.columns}


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


class ConfigBody(BaseModel):
    patch: dict[str, Any]


class PhraseBody(BaseModel):
    phrase: str = ""
    reason: str = ""
    fingerprint: str = ""
    action: str = ""


def create_app(*, engine: Any, repo: Any, settings: Settings, config_store: ConfigStore) -> FastAPI:
    auth = AuthManager(settings)

    app = FastAPI(title="POLYBOT", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # Some browsers do not treat 'self' as covering ws:/wss: for the page's own host, which
        # would block the live stream when the dashboard is reached through a public hostname.
        host = request.headers.get("host", "")
        own_ws = f" ws://{host} wss://{host}" if _HOST_RE.fullmatch(host) else ""
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self' ws://127.0.0.1:* ws://localhost:*{own_ws}; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def _session(request: Request) -> Session:
        session = auth.decode(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        return session

    def _csrf(request: Request, session: Session = Depends(_session)) -> Session:
        if not auth.verify_csrf(session, request.headers.get(CSRF_HEADER)):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        return session

    def _actor(request: Request, session: Session) -> str:
        host = request.client.host if request.client else "local"
        return f"{session.subject}@{host}"

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "local"

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health", include_in_schema=False)
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "polybot"}

    # ------------------------------------------------------------------- auth
    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        try:
            session = await asyncio.to_thread(
                auth.login, body.username, body.password, ip=_client_ip(request)
            )
        except AuthError as exc:
            repo.audit("LOGIN_FAILED", actor=f"anonymous@{_client_ip(request)}")
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        token = auth.encode(session)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_max_age_seconds,
            httponly=True,
            samesite="strict",
            # Plain http on loopback, https when reached through the public (Render) hostname.
            secure=request.url.scheme == "https",
            path="/",
        )
        repo.audit("LOGIN", actor=_client_ip(request))
        return {"ok": True, "username": session.subject, "csrf": session.csrf}

    @app.post("/api/logout")
    async def logout(response: Response, session: Session = Depends(_csrf)) -> dict[str, Any]:
        await asyncio.to_thread(repo.audit, "LOGOUT", actor=session.subject)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/session")
    async def session_info(session: Session = Depends(_session)) -> dict[str, Any]:
        return {"authenticated": True, "username": session.subject, "csrf": session.csrf}

    # ------------------------------------------------------------------ state
    @app.get("/api/state")
    async def state(session: Session = Depends(_session)) -> dict[str, Any]:
        snapshot = engine.state.snapshot()
        return {
            "ok": True,
            "state": redact(snapshot),
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    @app.websocket("/api/stream")
    async def stream(ws: WebSocket) -> None:
        """Push engine state to the dashboard the moment it changes (no polling lag)."""
        token = ws.cookies.get(SESSION_COOKIE)
        origin = ws.headers.get("origin")
        if auth.decode(token) is None or (origin and urlparse(origin).netloc != ws.headers.get("host")):
            await ws.close(code=1008)
            return
        await ws.accept()
        gone = asyncio.Event()

        async def drain() -> None:  # notice when the browser goes away
            try:
                while True:
                    await ws.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                gone.set()

        drain_task = asyncio.create_task(drain())
        last_version: Any = object()
        last_sent = 0.0
        last_auth = time.monotonic()
        try:
            while not gone.is_set():
                now = time.monotonic()
                if now - last_auth > 5:
                    last_auth = now
                    if auth.decode(token) is None:
                        await ws.close(code=1008)
                        return
                version = getattr(engine.state, "version", None)
                changed = version is None or version != last_version
                # Cap at ~20 pushes/s; always send at least every 2s so the client sees it is alive.
                if (changed and now - last_sent >= 0.05) or now - last_sent >= 2.0:
                    last_version = version
                    last_sent = now
                    await ws.send_json({
                        "ok": True,
                        "state": redact(engine.state.snapshot()),
                        "server_time": datetime.now(timezone.utc).isoformat(),
                    })
                await asyncio.sleep(0.02)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            drain_task.cancel()

    @app.get("/api/decisions")
    async def decisions(limit: int = 100, session: Session = Depends(_session)) -> dict[str, Any]:
        rows = await asyncio.to_thread(repo.recent_decisions, max(1, min(limit, 500)))
        return {"ok": True, "decisions": [_row(r) for r in rows]}

    @app.get("/api/orders")
    async def orders(limit: int = 100, session: Session = Depends(_session)) -> dict[str, Any]:
        rows = await asyncio.to_thread(repo.list_orders, limit=max(1, min(limit, 500)))
        return {"ok": True, "orders": [_row(r) for r in rows]}

    @app.get("/api/positions")
    async def positions(limit: int = 100, session: Session = Depends(_session)) -> dict[str, Any]:
        rows = await asyncio.to_thread(repo.list_positions, limit=max(1, min(limit, 500)))
        return {"ok": True, "positions": [_row(r) for r in rows]}

    @app.get("/api/trades")
    async def trades(limit: int = 200, session: Session = Depends(_session)) -> dict[str, Any]:
        rows = await asyncio.to_thread(repo.list_trades, limit=max(1, min(limit, 1000)))
        return {"ok": True, "trades": [_row(r) for r in rows]}

    @app.get("/api/rules")
    async def rules(session: Session = Depends(_session)) -> dict[str, Any]:
        rows = await asyncio.to_thread(repo.list_resolution_rules, 50)
        return {"ok": True, "rules": [_row(r) for r in rows]}

    @app.get("/api/resolutions")
    async def resolutions(limit: int = 100, session: Session = Depends(_session)) -> dict[str, Any]:
        rows = await asyncio.to_thread(repo.list_resolutions, max(1, min(limit, 500)))
        return {"ok": True, "resolutions": [_row(r) for r in rows]}

    @app.get("/api/chart")
    async def chart(seconds: int = 180, session: Session = Depends(_session)) -> dict[str, Any]:
        """Recent Chainlink TWAP ticks as [epoch_ms, price] pairs for the dashboard hero chart."""
        span = max(1, min(seconds, 600))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=span)
        points = [
            [int(o.ts.timestamp() * 1000), float(o.value)]
            for o in engine.tracker.observations_between(cutoff, now)
            if o.is_chainlink
        ]
        return {"ok": True, "points": points}

    @app.get("/api/analytics")
    async def analytics(session: Session = Depends(_session)) -> dict[str, Any]:
        def _build() -> dict[str, Any]:
            rows = repo.list_trades(limit=100_000)
            paper = [t for t in rows if t.mode == "PAPER"]
            live = [t for t in rows if t.mode == "LIVE"]
            predictions = _calibration_predictions(repo)
            report = build_validation_report(repo, config_store.snapshot())
            return {
                "paper": compute_performance(paper).as_dict(),
                "live": compute_performance(live).as_dict(),
                "daily": daily_performance(paper),
                "loss_breakdown": loss_breakdown(paper),
                "calibration": calibration_report(predictions).as_dict(),
                "validation": report.as_dict(),
            }

        return {"ok": True, "analytics": redact(await asyncio.to_thread(_build))}

    @app.get("/api/config")
    async def get_config(session: Session = Depends(_session)) -> dict[str, Any]:
        cfg = config_store.snapshot()
        return {"ok": True, "config": cfg.model_dump(mode="json"), "revision": config_store.revision}

    @app.post("/api/config")
    async def set_config(
        body: ConfigBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        unknown = set(body.patch) - CONFIG_WRITABLE
        if unknown:
            raise HTTPException(status_code=400, detail=f"fields not writable: {sorted(unknown)}")
        try:
            old, new = await asyncio.to_thread(config_store.update, body.patch)
        except HardLimitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid configuration: {exc}") from exc
        await asyncio.to_thread(
            repo.audit,
            "CONFIG_UPDATE",
            actor=_actor(request, session),
            detail={"changed": sorted(body.patch)},
        )
        return {"ok": True, "config": new.model_dump(mode="json"), "revision": config_store.revision}

    @app.get("/api/gate")
    async def gate(session: Session = Depends(_session)) -> dict[str, Any]:
        report = await engine.refresh_gate()
        return {"ok": True, "gate": report.as_dict()}

    # --------------------------------------------------------------- controls
    @app.post("/api/control/kill")
    async def kill(
        body: PhraseBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        action = "clear" if body.action == "clear" else "engage"
        phrase = PHRASES["clear_kill"] if action == "clear" else PHRASES["kill"]
        if body.phrase.strip() != phrase:
            raise HTTPException(status_code=400, detail=f"typed confirmation must be '{phrase}'")
        actor = _actor(request, session)
        if action == "clear":
            await asyncio.to_thread(engine.risk.clear_kill_switch, actor=actor, reason=body.reason)
        else:
            await asyncio.to_thread(engine.risk.engage_kill_switch, body.reason or "operator", actor=actor)
        return {"ok": True, "action": action}

    @app.post("/api/control/reset-day")
    async def reset_day(
        body: PhraseBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        if body.phrase.strip() != PHRASES["reset_day"]:
            raise HTTPException(status_code=400, detail="typed confirmation must be 'RESET DAY'")
        result = await asyncio.to_thread(
            engine.risk.reset_day_lock, actor=_actor(request, session), reason=body.reason or "operator"
        )
        return {"ok": True, **(result if isinstance(result, dict) else {})}

    @app.post("/api/activity/clear")
    async def clear_activity(
        body: PhraseBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        if body.action not in {"decisions", "orders", "positions", "trades"}:
            raise HTTPException(status_code=400, detail="unknown activity tab")
        removed = await asyncio.to_thread(repo.clear_activity, body.action)
        await asyncio.to_thread(
            repo.audit,
            "ACTIVITY_CLEARED",
            actor=_actor(request, session),
            detail={"tab": body.action, "rows": removed},
        )
        return {"ok": True, "tab": body.action, "removed": removed}

    @app.post("/api/rules/approve")
    async def approve_rule(
        body: PhraseBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        if body.phrase.strip() != PHRASES["approve_rule"]:
            raise HTTPException(status_code=400, detail="typed confirmation must be 'APPROVE RULE'")
        rule = await asyncio.to_thread(repo.get_resolution_rule, body.fingerprint)
        if rule is None:
            raise HTTPException(status_code=404, detail="rule not found")
        actor = _actor(request, session)
        await asyncio.to_thread(repo.approve_resolution_rule, body.fingerprint, actor)
        await asyncio.to_thread(
            repo.audit, "RESOLUTION_RULE_APPROVED", actor=actor, detail={"fingerprint": body.fingerprint}
        )
        return {"ok": True}

    @app.post("/api/live/activate")
    async def activate(
        body: PhraseBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        report = await engine.activate_live(typed_phrase=body.phrase, actor=_actor(request, session))
        payload = report.as_dict()
        if not report.passed:
            await asyncio.to_thread(
                repo.audit,
                "LIVE_ACTIVATION_FAILED",
                actor=_actor(request, session),
                detail={"failed": [c.key for c in report.failed]},
            )
        return {"ok": report.passed, "gate": payload}

    @app.post("/api/live/revoke")
    async def revoke(
        body: PhraseBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        if body.phrase.strip() != PHRASES["revoke_live"]:
            raise HTTPException(status_code=400, detail="typed confirmation must be 'REVOKE LIVE'")
        await engine.revoke_live(actor=_actor(request, session), reason=body.reason or "operator")
        return {"ok": True}

    @app.post("/api/live/heartbeat-test")
    async def heartbeat_test(
        body: PhraseBody, request: Request, session: Session = Depends(_csrf)
    ) -> dict[str, Any]:
        result = await engine.arm_heartbeat_test(typed_phrase=body.phrase, actor=_actor(request, session))
        return result

    # ------------------------------------------------------------------ pages
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Any) -> JSONResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        return FileResponse(WEB_DIR / "index.html")

    app.state.auth = auth
    app.state.engine = engine
    app.state.settings = settings
    app.state.started_at = time.time()
    return app
