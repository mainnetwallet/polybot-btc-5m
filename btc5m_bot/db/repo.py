"""Persistence operations. All financial values stay Decimal end-to-end."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..types import MarketInfo, OrderIntent, OrderStatus
from .engine import session_scope
from .models import (
    AppStateRow,
    AuditLogRow,
    DecisionLogRow,
    EquitySnapshotRow,
    ExposureReservationRow,
    HeartbeatStateRow,
    LatencySampleRow,
    LiveActivationRow,
    MarketRow,
    MarketWindowRow,
    OrderRow,
    PositionRow,
    ProtectionEventRow,
    RecoveryStateRow,
    ResolutionRow,
    ResolutionRuleRow,
    RiskDayRow,
    RiskStateRow,
    TradeRow,
    TwapObservationRow,
    _utcnow,
)


class DuplicateOrderError(RuntimeError):
    pass


class Repository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ markets
    def upsert_market(self, info: MarketInfo) -> None:
        with session_scope(self._sf) as s:
            row = s.get(MarketRow, info.market_id)
            values = dict(
                condition_id=info.condition_id,
                slug=info.slug,
                question=info.question,
                version=info.version.value,
                neg_risk=info.neg_risk,
                event_start=info.event_start,
                end_date=info.end_date,
                tick_size=info.tick_size,
                min_order_size=info.min_order_size,
                accepting_orders=info.accepting_orders,
                fees_enabled=info.fees_enabled,
                fee_rate=info.fee_rate,
                fee_exponent=info.fee_exponent,
                taker_only=info.taker_only,
                rebate_rate=info.rebate_rate,
                clob_token_ids=list(info.clob_token_ids),
                position_ids=list(info.position_ids),
                outcomes=list(info.outcomes),
                resolution_source=info.resolution_source,
                description=info.description,
                methodology=info.methodology.value,
                rules_fingerprint=info.rules_fingerprint,
                liquidity=info.liquidity,
                raw=info.raw,
                updated_at=_utcnow(),
            )
            if row is None:
                row = MarketRow(market_id=info.market_id, discovered_at=_utcnow(), **values)
                s.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    def get_market(self, market_id: str) -> MarketRow | None:
        with session_scope(self._sf) as s:
            return s.get(MarketRow, market_id)

    def recent_markets(self, limit: int = 20) -> list[MarketRow]:
        with session_scope(self._sf) as s:
            return list(s.scalars(select(MarketRow).order_by(MarketRow.event_start.desc()).limit(limit)))

    def ensure_window(self, market_id: str, entry_deadline: datetime) -> MarketWindowRow:
        with session_scope(self._sf) as s:
            row = s.scalar(select(MarketWindowRow).where(MarketWindowRow.market_id == market_id))
            if row is None:
                row = MarketWindowRow(market_id=market_id, entry_deadline=entry_deadline, state="DISCOVERED")
                s.add(row)
                s.flush()
            return row

    def get_window(self, market_id: str) -> MarketWindowRow | None:
        with session_scope(self._sf) as s:
            return s.scalar(select(MarketWindowRow).where(MarketWindowRow.market_id == market_id))

    def set_window_state(self, market_id: str, state: str, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.scalar(select(MarketWindowRow).where(MarketWindowRow.market_id == market_id))
            if row is None:
                raise KeyError(f"window not found: {market_id}")
            row.state = state
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown window field: {key}")
                setattr(row, key, value)
            row.updated_at = _utcnow()

    def update_window(self, market_id: str, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.scalar(select(MarketWindowRow).where(MarketWindowRow.market_id == market_id))
            if row is None:
                raise KeyError(f"window not found: {market_id}")
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown window field: {key}")
                setattr(row, key, value)
            row.updated_at = _utcnow()

    def recent_windows(self, limit: int = 20) -> list[MarketWindowRow]:
        with session_scope(self._sf) as s:
            return list(s.scalars(select(MarketWindowRow).order_by(MarketWindowRow.id.desc()).limit(limit)))

    # ------------------------------------------------------------------- orders
    def create_order_intent(self, intent: OrderIntent, status: OrderStatus = OrderStatus.INTENDED) -> None:
        with session_scope(self._sf) as s:
            row = OrderRow(
                client_order_id=intent.client_order_id,
                idempotency_key=intent.idempotency_key,
                market_id=intent.market_id,
                condition_id=intent.condition_id,
                asset_id=intent.asset_id,
                side=intent.side.value,
                outcome=intent.outcome.value,
                price=intent.limit_price,
                shares=intent.shares,
                order_type=intent.order_type,
                mode=intent.mode.value,
                status=status.value,
                strategy_version=intent.strategy_version,
                reason=intent.reason,
                intent_json={
                    "client_order_id": intent.client_order_id,
                    "market_id": intent.market_id,
                    "asset_id": intent.asset_id,
                    "side": intent.side.value,
                    "outcome": intent.outcome.value,
                    "shares": str(intent.shares),
                    "limit_price": str(intent.limit_price),
                    "order_type": intent.order_type,
                    "created_at": intent.created_at.isoformat(),
                },
                created_at=intent.created_at,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError as exc:
                raise DuplicateOrderError(intent.idempotency_key) from exc

    def update_order(self, client_order_id: str, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.get(OrderRow, client_order_id)
            if row is None:
                raise KeyError(client_order_id)
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown order field: {key}")
                setattr(row, key, value)

    def get_order(self, client_order_id: str) -> OrderRow | None:
        with session_scope(self._sf) as s:
            return s.get(OrderRow, client_order_id)

    def list_orders(
        self,
        *,
        market_id: str | None = None,
        statuses: Sequence[str] | None = None,
        mode: str | None = None,
        limit: int = 100,
    ) -> list[OrderRow]:
        stmt = select(OrderRow).order_by(OrderRow.created_at.desc()).limit(limit)
        if market_id:
            stmt = stmt.where(OrderRow.market_id == market_id)
        if statuses:
            stmt = stmt.where(OrderRow.status.in_(list(statuses)))
        if mode:
            stmt = stmt.where(OrderRow.mode == mode)
        with session_scope(self._sf) as s:
            return list(s.scalars(stmt))

    def count_orders(self, *, market_id: str, mode: str, side: str) -> int:
        """How many orders (any status) exist for this market/mode/side. Used to number exit attempts."""
        stmt = (
            select(func.count())
            .select_from(OrderRow)
            .where(OrderRow.market_id == market_id, OrderRow.mode == mode, OrderRow.side == side)
        )
        with session_scope(self._sf) as s:
            return int(s.scalar(stmt) or 0)

    def open_orders(self, mode: str | None = None) -> list[OrderRow]:
        statuses = [
            OrderStatus.INTENDED.value,
            OrderStatus.SUBMITTING.value,
            OrderStatus.ACKED.value,
            OrderStatus.PENDING.value,
            OrderStatus.PARTIAL.value,
            OrderStatus.CANCEL_PENDING.value,
            OrderStatus.UNKNOWN.value,
        ]
        # A FOK/FAK partial cannot receive further fills: the exchange cancels the
        # remainder. It is a terminal record, not a working order.
        stmt = (
            select(OrderRow)
            .where(
                OrderRow.status.in_(statuses),
                or_(
                    OrderRow.status != OrderStatus.PARTIAL.value,
                    OrderRow.order_type.notin_(["FOK", "FAK"]),
                ),
            )
            .order_by(OrderRow.created_at.desc())
            .limit(500)
        )
        if mode:
            stmt = stmt.where(OrderRow.mode == mode)
        with session_scope(self._sf) as s:
            return list(s.scalars(stmt))

    # ---------------------------------------------------------------- positions
    def get_position(self, market_id: str, mode: str) -> PositionRow | None:
        with session_scope(self._sf) as s:
            return s.scalar(
                select(PositionRow).where(PositionRow.market_id == market_id, PositionRow.mode == mode)
            )

    def open_position(self, mode: str) -> PositionRow | None:
        with session_scope(self._sf) as s:
            return s.scalar(
                select(PositionRow).where(
                    PositionRow.mode == mode,
                    PositionRow.status.in_(["OPEN", "DUST"]),
                )
            )

    def create_position(
        self,
        *,
        market_id: str,
        condition_id: str,
        asset_id: str,
        outcome: str,
        shares: Decimal,
        avg_price: Decimal,
        entry_fee: Decimal,
        mode: str,
    ) -> PositionRow:
        with session_scope(self._sf) as s:
            row = PositionRow(
                market_id=market_id,
                condition_id=condition_id,
                asset_id=asset_id,
                outcome=outcome,
                shares=shares,
                avg_price=avg_price,
                entry_fee=entry_fee,
                cost_basis=shares * avg_price + entry_fee,
                mode=mode,
            )
            s.add(row)
            s.flush()
            return row

    def reconcile_sell_fill(
        self,
        *,
        client_order_id: str,
        position_id: int,
        status: str,
        total_filled: Decimal,
        total_avg_price: Decimal,
        total_fee: Decimal,
        delta_filled: Decimal,
        delta_avg_price: Decimal,
        delta_fee: Decimal,
        delta_realized_pnl: Decimal,
        remaining_shares: Decimal,
        remaining_cost_basis: Decimal,
        remaining_entry_fee: Decimal,
        filled_at: datetime,
        cancelled_at: datetime | None,
    ) -> bool:
        """Atomically persist a recovered SELL fill, position accounting and idempotency marker."""
        marker_key = f"sell_recovery:{client_order_id}"
        with session_scope(self._sf) as s:
            order = s.get(OrderRow, client_order_id)
            position = s.get(PositionRow, position_id)
            if order is None or position is None:
                raise KeyError("recovery order or position disappeared")
            if s.get(AppStateRow, marker_key) is not None:
                return False
            order.status = status
            order.filled_shares = total_filled
            order.avg_price = total_avg_price
            order.fee_usd = total_fee
            order.filled_at = filled_at
            order.cancelled_at = cancelled_at
            position.shares = remaining_shares
            position.cost_basis = remaining_cost_basis
            position.entry_fee = remaining_entry_fee
            position.exit_price = delta_avg_price if delta_filled > 0 else position.exit_price
            position.exit_fee = position.exit_fee + delta_fee
            position.realized_pnl = position.realized_pnl + delta_realized_pnl
            if remaining_shares <= Decimal("0.001"):
                position.shares = Decimal(0)
                position.cost_basis = Decimal(0)
                position.entry_fee = Decimal(0)
                position.status = "CLOSED"
                position.closed_at = filled_at
            if delta_filled > 0:
                s.add(TradeRow(
                    market_id=order.market_id, client_order_id=client_order_id,
                    side="SELL", outcome=order.outcome, price=delta_avg_price,
                    shares=delta_filled, fee=delta_fee, realized_pnl=delta_realized_pnl,
                    kind="EXIT_RECOVERY", mode=order.mode,
                ))
            reservation = s.scalar(
                select(ExposureReservationRow).where(
                    ExposureReservationRow.client_order_id == client_order_id,
                    ExposureReservationRow.status == "ACTIVE",
                )
            )
            if reservation is not None:
                reservation.status = "CONSUMED"
                reservation.released_at = _utcnow()
            s.add(AppStateRow(
                key=marker_key,
                value={"status": status, "filled_shares": str(total_filled), "at": filled_at.isoformat()},
            ))
            return True

    def update_position(self, position_id: int, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.get(PositionRow, position_id)
            if row is None:
                raise KeyError(position_id)
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown position field: {key}")
                setattr(row, key, value)

    def list_positions(self, *, mode: str | None = None, limit: int = 100) -> list[PositionRow]:
        stmt = select(PositionRow).order_by(PositionRow.id.desc()).limit(limit)
        if mode:
            stmt = stmt.where(PositionRow.mode == mode)
        with session_scope(self._sf) as s:
            return list(s.scalars(stmt))

    def settled_position_for_market(self, market_id: str, mode: str) -> PositionRow | None:
        with session_scope(self._sf) as s:
            return s.scalar(
                select(PositionRow).where(
                    PositionRow.market_id == market_id,
                    PositionRow.mode == mode,
                    PositionRow.status.in_(["SETTLED", "REDEEMED", "CLOSED"]),
                )
            )

    # ------------------------------------------------------------- reservations
    def reserve(self, market_id: str, client_order_id: str, amount: Decimal, note: str = "") -> None:
        with session_scope(self._sf) as s:
            s.add(
                ExposureReservationRow(
                    market_id=market_id,
                    client_order_id=client_order_id,
                    amount=amount,
                    status="ACTIVE",
                    note=note,
                )
            )

    def release_reservation(self, client_order_id: str, status: str = "RELEASED") -> None:
        with session_scope(self._sf) as s:
            row = s.scalar(
                select(ExposureReservationRow).where(
                    ExposureReservationRow.client_order_id == client_order_id,
                    ExposureReservationRow.status == "ACTIVE",
                )
            )
            if row:
                row.status = status
                row.released_at = _utcnow()

    def active_reserved_total(self) -> Decimal:
        with session_scope(self._sf) as s:
            amounts = s.scalars(
                select(ExposureReservationRow.amount).where(ExposureReservationRow.status == "ACTIVE")
            ).all()
        # amount is stored as exact decimal text; summing in Python is exact and works on Postgres,
        # where SUM(varchar) is an error (SQLite silently coerced it to a float).
        return sum((Decimal(str(a)) for a in amounts if a is not None), Decimal("0"))

    def active_reservations(self) -> list[ExposureReservationRow]:
        with session_scope(self._sf) as s:
            return list(s.scalars(select(ExposureReservationRow).where(ExposureReservationRow.status == "ACTIVE")))

    # --------------------------------------------------------------------- risk
    def get_risk_day(self, day: date | None = None) -> RiskDayRow:
        day = day or datetime.now(timezone.utc).date()
        with session_scope(self._sf) as s:
            row = s.get(RiskDayRow, day)
            if row is None:
                row = RiskDayRow(trading_day=day)
                s.add(row)
                s.flush()
            return row

    def update_risk_day(self, day: date, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.get(RiskDayRow, day)
            if row is None:
                row = RiskDayRow(trading_day=day)
                s.add(row)
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown risk day field: {key}")
                setattr(row, key, value)
            row.updated_at = _utcnow()

    def get_risk_state(self) -> RiskStateRow:
        with session_scope(self._sf) as s:
            row = s.get(RiskStateRow, 1)
            if row is None:
                row = RiskStateRow(id=1)
                s.add(row)
                s.flush()
            return row

    def update_risk_state(self, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.get(RiskStateRow, 1)
            if row is None:
                row = RiskStateRow(id=1)
                s.add(row)
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown risk state field: {key}")
                setattr(row, key, value)
            row.updated_at = _utcnow()

    def add_protection_event(self, event_type: str, severity: str = "INFO", **details: Any) -> None:
        with session_scope(self._sf) as s:
            s.add(ProtectionEventRow(event_type=event_type, severity=severity, details=details))

    def recent_protection_events(self, limit: int = 50) -> list[ProtectionEventRow]:
        with session_scope(self._sf) as s:
            return list(s.scalars(select(ProtectionEventRow).order_by(ProtectionEventRow.id.desc()).limit(limit)))

    # --------------------------------------------------------------- decisions
    def log_decision(
        self,
        *,
        market_id: str,
        decision: str,
        reason_code: str,
        reason_detail: str = "",
        probability: Decimal | None = None,
        edge: Decimal | None = None,
        price: Decimal | None = None,
        regime: str | None = None,
        features: dict[str, Any] | None = None,
        mode: str = "PAPER",
        ts: datetime | None = None,
    ) -> None:
        with session_scope(self._sf) as s:
            s.add(
                DecisionLogRow(
                    market_id=market_id,
                    ts=ts or _utcnow(),
                    decision=decision,
                    reason_code=reason_code,
                    reason_detail=reason_detail,
                    probability=probability,
                    edge=edge,
                    price=price,
                    regime=regime,
                    features=features or {},
                    mode=mode,
                )
            )

    def recent_decisions(self, limit: int = 100, market_id: str | None = None) -> list[DecisionLogRow]:
        stmt = select(DecisionLogRow).order_by(DecisionLogRow.id.desc()).limit(limit)
        if market_id:
            stmt = stmt.where(DecisionLogRow.market_id == market_id)
        with session_scope(self._sf) as s:
            return list(s.scalars(stmt))

    def clear_activity(self, kind: str) -> int:
        """Delete history shown on the dashboard Activity page. Returns the rows removed.

        Only inert history is removed: decision logs, and PAPER orders / positions / trades that
        are finished. Open or in-flight rows and everything recorded in LIVE mode are kept.
        """
        if kind == "decisions":
            stmt = delete(DecisionLogRow)
        elif kind == "orders":
            terminal = ["FILLED", "CANCELLED", "REJECTED"]
            stmt = delete(OrderRow).where(OrderRow.mode == "PAPER", OrderRow.status.in_(terminal))
        elif kind == "positions":
            done = ["CLOSED", "SETTLED", "REDEEMED"]
            stmt = delete(PositionRow).where(PositionRow.mode == "PAPER", PositionRow.status.in_(done))
        elif kind == "trades":
            stmt = delete(TradeRow).where(TradeRow.mode == "PAPER")
        else:
            raise ValueError(f"unknown activity kind: {kind}")
        with session_scope(self._sf) as s:
            return int(s.execute(stmt).rowcount or 0)

    # ------------------------------------------------------------------ latency
    def add_latency(self, kind: str, ms: int, market_id: str | None = None) -> None:
        with session_scope(self._sf) as s:
            s.add(LatencySampleRow(kind=kind, ms=ms, market_id=market_id))

    def latency_stats(self, kind: str | None = None, limit: int = 1000) -> dict[str, float | int | None]:
        stmt = select(LatencySampleRow.ms).order_by(LatencySampleRow.id.desc()).limit(limit)
        if kind:
            stmt = stmt.where(LatencySampleRow.kind == kind)
        with session_scope(self._sf) as s:
            values = sorted(int(v) for v in s.scalars(stmt).all())
        if not values:
            return {"count": 0, "p50": None, "p95": None, "p99": None}
        def pct(p: float) -> float:
            idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
            return float(values[idx])
        return {"count": len(values), "p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}

    # ------------------------------------------------------------------- trades
    def add_trade(
        self,
        *,
        market_id: str,
        client_order_id: str,
        side: str,
        outcome: str,
        price: Decimal,
        shares: Decimal,
        fee: Decimal,
        realized_pnl: Decimal = Decimal(0),
        kind: str = "FILL",
        mode: str = "PAPER",
    ) -> None:
        with session_scope(self._sf) as s:
            s.add(
                TradeRow(
                    market_id=market_id,
                    client_order_id=client_order_id,
                    side=side,
                    outcome=outcome,
                    price=price,
                    shares=shares,
                    fee=fee,
                    realized_pnl=realized_pnl,
                    kind=kind,
                    mode=mode,
                )
            )

    def list_trades(self, *, mode: str | None = None, limit: int = 500) -> list[TradeRow]:
        stmt = select(TradeRow).order_by(TradeRow.id.desc()).limit(limit)
        if mode:
            stmt = stmt.where(TradeRow.mode == mode)
        with session_scope(self._sf) as s:
            return list(s.scalars(stmt))

    # --------------------------------------------------------------- resolution
    def upsert_resolution_rule(self, rule: dict[str, Any]) -> None:
        with session_scope(self._sf) as s:
            row = s.get(ResolutionRuleRow, rule["fingerprint"])
            if row is None:
                row = ResolutionRuleRow(**rule)
                s.add(row)
            else:
                for key, value in rule.items():
                    if key in {"approved", "approved_at", "approved_by"}:
                        continue
                    if hasattr(row, key):
                        setattr(row, key, value)

    def get_resolution_rule(self, fingerprint: str) -> ResolutionRuleRow | None:
        with session_scope(self._sf) as s:
            return s.get(ResolutionRuleRow, fingerprint)

    def list_resolution_rules(self, limit: int = 50) -> list[ResolutionRuleRow]:
        with session_scope(self._sf) as s:
            return list(
                s.scalars(
                    select(ResolutionRuleRow)
                    .order_by(ResolutionRuleRow.created_at.desc())
                    .limit(limit)
                )
            )

    def approve_resolution_rule(self, fingerprint: str, actor: str) -> None:
        with session_scope(self._sf) as s:
            row = s.get(ResolutionRuleRow, fingerprint)
            if row is None:
                raise KeyError(fingerprint)
            row.approved = True
            row.approved_at = _utcnow()
            row.approved_by = actor

    def approved_rule_count(self) -> int:
        with session_scope(self._sf) as s:
            return int(s.scalar(select(func.count()).select_from(ResolutionRuleRow).where(ResolutionRuleRow.approved)) or 0)

    def save_resolution(
        self,
        *,
        market_id: str,
        simulated_outcome: str | None,
        actual_outcome: str | None,
        opening_ref: Decimal | None,
        closing_ref: Decimal | None,
        confidence: str,
        methodology: str,
        observations: dict[str, Any],
        match: bool | None = None,
        validated: bool = False,
    ) -> None:
        with session_scope(self._sf) as s:
            row = s.scalar(select(ResolutionRow).where(ResolutionRow.market_id == market_id))
            if row is None:
                row = ResolutionRow(market_id=market_id)
                s.add(row)
            row.simulated_outcome = simulated_outcome
            row.actual_outcome = actual_outcome
            row.match = match
            row.opening_ref = opening_ref
            row.closing_ref = closing_ref
            row.confidence = confidence
            row.methodology = methodology
            row.observations = observations
            row.validated = validated

    def list_resolutions(self, limit: int = 100) -> list[ResolutionRow]:
        with session_scope(self._sf) as s:
            return list(s.scalars(select(ResolutionRow).order_by(ResolutionRow.id.desc()).limit(limit)))

    def get_resolution(self, market_id: str) -> ResolutionRow | None:
        with session_scope(self._sf) as s:
            return s.scalar(select(ResolutionRow).where(ResolutionRow.market_id == market_id))

    def record_actual_resolution(self, market_id: str, actual: str) -> tuple[bool | None, str | None, bool]:
        """Attach the venue's outcome to a simulation that was saved earlier (e.g. before a restart).

        The in-memory simulation is gone after a restart, but its result was already written to the
        ``resolutions`` row. Returns ``(match, simulated_outcome, changed)``; ``changed`` is False
        when this exact outcome was already recorded, so callers can avoid repeating alerts.
        A market with no saved simulation is left alone: there is nothing to compare against.
        """
        actual_norm = str(actual or "").upper()
        with session_scope(self._sf) as s:
            row = s.scalar(select(ResolutionRow).where(ResolutionRow.market_id == market_id))
            if row is None:
                return None, None, False
            if actual_norm in {"UP", "DOWN"} and row.simulated_outcome:
                match: bool | None = str(row.simulated_outcome).upper() == actual_norm
            else:
                match = None
            changed = row.actual_outcome != actual_norm or row.match != match
            row.actual_outcome = actual_norm or None
            row.match = match
            row.validated = match is True
            return match, row.simulated_outcome, changed

    def resolution_validation_count(self) -> tuple[int, int]:
        with session_scope(self._sf) as s:
            total = int(s.scalar(select(func.count()).select_from(ResolutionRow).where(ResolutionRow.match.is_not(None))) or 0)
            matched = int(s.scalar(select(func.count()).select_from(ResolutionRow).where(ResolutionRow.match.is_(True))) or 0)
        return total, matched

    # --------------------------------------------------------- oracle observations
    def add_twap_observations(self, rows: Iterable[dict[str, Any]]) -> int:
        batch = [
            TwapObservationRow(
                ts=r["ts"],
                value=r["value"],
                source=str(r.get("source") or "")[:64],
                symbol=str(r.get("symbol") or "btcusd")[:32],
                window_seconds=int(r.get("window_seconds") or 60),
                sequence=r.get("sequence"),
            )
            for r in rows
        ]
        if not batch:
            return 0
        with session_scope(self._sf) as s:
            s.add_all(batch)
        return len(batch)

    def recent_twap_observations(self, since: datetime, limit: int = 10_000) -> list[TwapObservationRow]:
        """Oldest first, newest ``limit`` rows at or after ``since``."""
        with session_scope(self._sf) as s:
            rows = list(
                s.scalars(
                    select(TwapObservationRow)
                    .where(TwapObservationRow.ts >= since)
                    .order_by(TwapObservationRow.ts.desc(), TwapObservationRow.id.desc())
                    .limit(limit)
                )
            )
        rows.reverse()
        return rows

    def prune_twap_observations(self, before: datetime) -> int:
        with session_scope(self._sf) as s:
            result = s.execute(delete(TwapObservationRow).where(TwapObservationRow.ts < before))
            return int(result.rowcount or 0)

    # ---------------------------------------------------------------- heartbeat
    def get_heartbeat_state(self) -> HeartbeatStateRow:
        with session_scope(self._sf) as s:
            row = s.get(HeartbeatStateRow, 1)
            if row is None:
                row = HeartbeatStateRow(id=1)
                s.add(row)
                s.flush()
            return row

    def update_heartbeat_state(self, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.get(HeartbeatStateRow, 1)
            if row is None:
                row = HeartbeatStateRow(id=1)
                s.add(row)
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown heartbeat field: {key}")
                setattr(row, key, value)
            row.updated_at = _utcnow()

    # ----------------------------------------------------------------- recovery
    def get_recovery_state(self) -> RecoveryStateRow:
        with session_scope(self._sf) as s:
            row = s.get(RecoveryStateRow, 1)
            if row is None:
                row = RecoveryStateRow(id=1)
                s.add(row)
                s.flush()
            return row

    def update_recovery_state(self, **fields: Any) -> None:
        with session_scope(self._sf) as s:
            row = s.get(RecoveryStateRow, 1)
            if row is None:
                row = RecoveryStateRow(id=1)
                s.add(row)
            for key, value in fields.items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown recovery field: {key}")
                setattr(row, key, value)
            row.updated_at = _utcnow()

    # ---------------------------------------------------------------- app state
    def set_state(self, key: str, value: dict[str, Any]) -> None:
        with session_scope(self._sf) as s:
            row = s.get(AppStateRow, key)
            if row is None:
                row = AppStateRow(key=key, value=value)
                s.add(row)
            else:
                row.value = value
                row.updated_at = _utcnow()

    def get_state(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        with session_scope(self._sf) as s:
            row = s.get(AppStateRow, key)
            return dict(row.value) if row else dict(default or {})

    # -------------------------------------------------------------------- audit
    def audit(self, action: str, *, actor: str = "system", detail: dict[str, Any] | None = None, ip: str | None = None) -> None:
        with session_scope(self._sf) as s:
            s.add(AuditLogRow(actor=actor, action=action, detail=detail or {}, ip=ip))

    def recent_audit(self, limit: int = 100) -> list[AuditLogRow]:
        with session_scope(self._sf) as s:
            return list(s.scalars(select(AuditLogRow).order_by(AuditLogRow.id.desc()).limit(limit)))

    # ----------------------------------------------------------- live activation
    def create_live_activation(self, phrase_hash: str, actor: str, config: dict[str, Any]) -> None:
        with session_scope(self._sf) as s:
            s.add(LiveActivationRow(phrase_hash=phrase_hash, actor=actor, config_json=config))

    def active_live_activation(self) -> LiveActivationRow | None:
        with session_scope(self._sf) as s:
            return s.scalar(
                select(LiveActivationRow)
                .where(LiveActivationRow.revoked_at.is_(None))
                .order_by(LiveActivationRow.id.desc())
            )

    def revoke_live_activation(self, actor: str) -> None:
        with session_scope(self._sf) as s:
            row = s.scalar(
                select(LiveActivationRow)
                .where(LiveActivationRow.revoked_at.is_(None))
                .order_by(LiveActivationRow.id.desc())
            )
            if row:
                row.revoked_at = _utcnow()
                row.revoked_by = actor

    # ------------------------------------------------------------------- equity
    def add_equity_snapshot(self, balance: Decimal, equity: Decimal, exposure: Decimal, daily_pnl: Decimal, mode: str) -> None:
        with session_scope(self._sf) as s:
            s.add(EquitySnapshotRow(balance=balance, equity=equity, exposure=exposure, daily_pnl=daily_pnl, mode=mode))

    def latest_equity(self, mode: str) -> EquitySnapshotRow | None:
        with session_scope(self._sf) as s:
            return s.scalar(
                select(EquitySnapshotRow).where(EquitySnapshotRow.mode == mode).order_by(EquitySnapshotRow.id.desc())
            )
