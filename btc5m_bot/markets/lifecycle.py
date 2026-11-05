"""Persistent market lifecycle with a hard 120-second entry cutoff."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ..types import MarketInfo, MarketState, as_utc

_RANK = {
    MarketState.DISCOVERED: 0,
    MarketState.ACTIVE: 1,
    MarketState.ENTRY_CLOSED: 2,
    MarketState.RESOLVING: 3,
    MarketState.SETTLED: 4,
    MarketState.ABORTED: 5,
}


@dataclass(frozen=True)
class LifecycleTransition:
    market_id: str
    old: MarketState
    new: MarketState
    at: datetime


class MarketStateMachine:
    def __init__(self, market: MarketInfo, state: MarketState = MarketState.DISCOVERED) -> None:
        self.market = market
        self.state = state

    @property
    def start(self) -> datetime:
        return as_utc(self.market.event_start)

    @property
    def end(self) -> datetime:
        return as_utc(self.market.end_date)

    @property
    def entry_deadline(self) -> datetime:
        # Immutable 120-second window regardless of user config.
        return self.start + timedelta(seconds=120)

    def expected_state(self, now: datetime) -> MarketState:
        now = as_utc(now)
        if now >= self.end:
            return MarketState.RESOLVING
        if now >= self.entry_deadline:
            return MarketState.ENTRY_CLOSED
        if now >= self.start:
            return MarketState.ACTIVE
        return MarketState.DISCOVERED

    def tick(self, now: datetime) -> LifecycleTransition | None:
        if self.state in {MarketState.SETTLED, MarketState.ABORTED}:
            return None
        expected = self.expected_state(now)
        if _RANK[expected] > _RANK[self.state]:
            old = self.state
            self.state = expected
            return LifecycleTransition(self.market.market_id, old, expected, as_utc(now))
        return None

    def settle(self, now: datetime, *, aborted: bool = False) -> LifecycleTransition | None:
        target = MarketState.ABORTED if aborted else MarketState.SETTLED
        if self.state is target:
            return None
        old = self.state
        self.state = target
        return LifecycleTransition(self.market.market_id, old, target, as_utc(now))

    def entry_allowed(self, now: datetime) -> bool:
        now = as_utc(now)
        return self.state is MarketState.ACTIVE and self.start <= now < self.entry_deadline

    def seconds_to_start(self, now: datetime) -> float:
        return (self.start - as_utc(now)).total_seconds()

    def seconds_remaining(self, now: datetime) -> float:
        return (self.end - as_utc(now)).total_seconds()

    def entry_seconds_remaining(self, now: datetime) -> float:
        return (self.entry_deadline - as_utc(now)).total_seconds()

    def on_transition(self, callback: Callable[[LifecycleTransition], None]) -> None:
        self._callback = callback
