"""Dynamic Gamma/CLOB market discovery. No market IDs or slugs are hardcoded."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from ..markets.rules import build_rule_spec
from ..types import MarketInfo, MarketVersion, Methodology, as_utc
from .base import ExchangeError

log = logging.getLogger(__name__)

CANDIDATE_SLUG = "btc-updown-5m-{epoch}"
SERIES_SLUG = "btc-up-or-down-5m"


class DiscoveryError(ExchangeError):
    pass


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        return as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except Exception:
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _num(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def parse_market(event: dict[str, Any], market: dict[str, Any]) -> MarketInfo:
    event_start = _parse_dt(market.get("eventStartTime") or event.get("startTime"))
    end_date = _parse_dt(market.get("endDate") or event.get("endDate"))
    if event_start is None or end_date is None:
        raise DiscoveryError("market is missing eventStartTime/endDate")
    version_raw = market.get("version") or event.get("version")
    version = MarketVersion.parse(version_raw)
    condition_id = str(market.get("conditionId") or "")
    market_id = str(market.get("id") or "")
    slug = str(market.get("slug") or event.get("slug") or "")
    if not condition_id or not market_id:
        raise DiscoveryError("market is missing id/conditionId")

    fee_schedule = market.get("feeSchedule") or {}
    fees_enabled = bool(market.get("feesEnabled"))
    fee_rate = _num(fee_schedule.get("rate"))
    fee_verified = (not fees_enabled) or ("rate" in fee_schedule and fee_rate >= 0)
    if fees_enabled and "rate" not in fee_schedule:
        fee_verified = False

    spec = build_rule_spec(market, event_start, version.value)
    outcomes = tuple(str(x) for x in _as_list(market.get("outcomes"))) or ("Up", "Down")
    clob_ids = tuple(str(x) for x in _as_list(market.get("clobTokenIds")))
    position_ids = tuple(str(x) for x in _as_list(market.get("positionIds")))

    tick = _num(market.get("orderPriceMinTickSize"), "0.01")
    min_size = _num(market.get("orderMinSize"), "5")
    if tick <= 0 or min_size <= 0:
        raise DiscoveryError("invalid tick or minimum order size")

    return MarketInfo(
        market_id=market_id,
        condition_id=condition_id,
        slug=slug,
        question=str(market.get("question") or event.get("title") or ""),
        version=version,
        neg_risk=bool(market.get("negRisk")),
        event_start=event_start,
        end_date=end_date,
        clob_token_ids=clob_ids,
        position_ids=position_ids,
        outcomes=outcomes,
        tick_size=tick,
        min_order_size=min_size,
        accepting_orders=bool(market.get("acceptingOrders")),
        fees_enabled=fees_enabled,
        fee_rate=fee_rate,
        fee_exponent=int(fee_schedule.get("exponent", 1)),
        taker_only=bool(fee_schedule.get("takerOnly", True)),
        rebate_rate=_num(fee_schedule.get("rebateRate")),
        resolution_source=str(market.get("resolutionSource") or ""),
        description=str(market.get("description") or ""),
        methodology=spec.methodology,
        rules_fingerprint=spec.fingerprint,
        liquidity=_num(market.get("liquidityNum") or market.get("liquidity")),
        fee_verified=fee_verified,
        raw={"event": event, "market": market, "rule_spec": {
            "confidence": spec.confidence,
            "warnings": list(spec.warnings),
            "tie_rule": spec.tie_rule,
            "precision": spec.precision,
            "rounding": spec.rounding,
            "source": spec.source,
        }},
    )


class GammaClient:
    """Public discovery client. Isolated behind the exchange boundary."""

    def __init__(self, base_url: str, *, timeout: float = 20.0, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "POLYBOT/1.0 (+local research; paper-first)"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, retries: int = 2) -> Any:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await self._client.get(f"{self.base_url}{path}", params=params)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # network / malformed JSON
                last = exc
                if attempt < retries:
                    await asyncio.sleep(0.35 * (attempt + 1))
        raise DiscoveryError(f"Gamma request failed: {path}: {last}")

    async def get_event_by_slug(self, slug: str) -> dict[str, Any] | None:
        return await self._get(f"/events/slug/{slug}")

    async def _market_from_event(self, event: dict[str, Any]) -> MarketInfo | None:
        markets = event.get("markets") or []
        if not markets:
            return None
        slug = str(event.get("slug") or "")
        for raw in markets:
            if not isinstance(raw, dict):
                continue
            if raw.get("closed") or raw.get("archived"):
                continue
            try:
                info = parse_market(event, raw)
            except DiscoveryError as exc:
                log.warning("skipping malformed market in %s: %s", slug, exc)
                continue
            if info.slug and info.slug != slug:
                # event may aggregate several markets; keep only the 5m UP/DOWN market
                if not info.slug.startswith("btc-updown-5m-"):
                    continue
            return info
        return None

    async def discover(self, now: datetime, *, lookahead_windows: int = 4) -> list[MarketInfo]:
        now = as_utc(now)
        epoch = int(now.timestamp()) // 300 * 300
        epochs = [epoch + 300 * i for i in range(-1, lookahead_windows + 1)]
        results: dict[str, MarketInfo] = {}

        async def probe(ep: int) -> None:
            try:
                event = await self.get_event_by_slug(CANDIDATE_SLUG.format(epoch=ep))
            except DiscoveryError as exc:
                log.debug("candidate probe failed for %s: %s", ep, exc)
                return
            if not isinstance(event, dict):
                return
            info = await self._market_from_event(event)
            if info is not None:
                results[info.condition_id] = info

        await asyncio.gather(*(probe(ep) for ep in epochs))

        if not results:
            # Fallback: resolve the series dynamically (no fixed market identifiers).
            series = await self._get("/series", params={"slug": SERIES_SLUG})
            if isinstance(series, list) and series:
                for event in series[0].get("events") or []:
                    if not isinstance(event, dict):
                        continue
                    start = _parse_dt(event.get("startTime") or event.get("startDate"))
                    if start is None or abs((start - now).total_seconds()) > 3600:
                        continue
                    info = await self._market_from_event(event)
                    if info is not None:
                        results[info.condition_id] = info

        markets = sorted(results.values(), key=lambda m: m.event_start)
        # never trade stale windows; keep one previous window only for settlement reconciliation
        cutoff = now - timedelta(seconds=300)
        return [m for m in markets if m.end_date >= cutoff]

    async def get_market_by_slug(self, slug: str) -> MarketInfo | None:
        event = await self.get_event_by_slug(slug)
        if not isinstance(event, dict):
            return None
        return await self._market_from_event(event)

    async def server_time(self) -> datetime:
        response = await self._client.get(f"{self.base_url}/time")
        response.raise_for_status()
        return datetime.fromtimestamp(int(response.text.strip()), tz=timezone.utc)
