"""False-signal filters. UNAVAILABLE is never treated as a pass."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from ..types import FilterResult, FilterVerdict
from .features import FeatureSnapshot


@dataclass
class FilterReport:
    results: list[FilterResult] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return bool(self.results) and all(r.verdict is FilterVerdict.PASS for r in self.results)

    @property
    def failures(self) -> list[FilterResult]:
        return [r for r in self.results if r.verdict is FilterVerdict.FAIL]

    @property
    def unavailable(self) -> list[FilterResult]:
        return [r for r in self.results if r.verdict is FilterVerdict.UNAVAILABLE]

    @property
    def summary(self) -> str:
        bad = self.failures + self.unavailable
        if not bad:
            return "all filters passed"
        return "; ".join(f"{r.name}={r.verdict.value}({r.detail})" for r in bad)


class FilterEngine:
    def __init__(
        self,
        *,
        max_spread: Decimal = Decimal("0.03"),
        min_depth_multiple: Decimal = Decimal("3.0"),
        max_oracle_basis_bps: float = 30.0,
        high_vol: float = 0.0012,
    ) -> None:
        self.max_spread = max_spread
        self.min_depth_multiple = min_depth_multiple
        self.max_oracle_basis_bps = max_oracle_basis_bps
        self.high_vol = high_vol

    def evaluate(self, f: FeatureSnapshot, *, required_shares: Decimal) -> FilterReport:
        results = [
            self._fake_breakout(f),
            self._momentum_exhaustion(f),
            self._sudden_reversal(f),
            self._volume_confirmation(f),
            self._oracle_divergence(f),
            self._abnormal_order_book(f),
            self._liquidity_withdrawal(f, required_shares),
            self._spread_expansion(f),
        ]
        return FilterReport(results)

    def _fake_breakout(self, f: FeatureSnapshot) -> FilterResult:
        r15 = f.returns.get(15)
        vol = f.realized_vol_60s
        if r15 is None or vol is None or f.volume_180s <= 0:
            return FilterResult("fake_breakout", FilterVerdict.UNAVAILABLE, "missing 15s return/volume")
        z = abs(r15) / (vol * math.sqrt(15)) if vol > 0 else 0.0
        expected_30 = float(f.volume_180s) / 6.0
        if z >= 2.0 and expected_30 > 0 and float(f.volume_30s) < expected_30 * 0.45:
            return FilterResult("fake_breakout", FilterVerdict.FAIL, f"breakout z={z:.2f} without volume", score=z)
        return FilterResult("fake_breakout", FilterVerdict.PASS, f"z={z:.2f}", score=z)

    def _momentum_exhaustion(self, f: FeatureSnapshot) -> FilterResult:
        r60 = f.returns.get(60)
        acc = f.acceleration
        if r60 is None or acc is None:
            return FilterResult("momentum_exhaustion", FilterVerdict.UNAVAILABLE, "missing acceleration")
        magnitude = abs(acc) * 60.0
        if r60 > 0 and acc < 0 and magnitude > abs(r60) * 0.40:
            return FilterResult("momentum_exhaustion", FilterVerdict.FAIL, "up momentum decelerating sharply")
        if r60 < 0 and acc > 0 and magnitude > abs(r60) * 0.40:
            return FilterResult("momentum_exhaustion", FilterVerdict.FAIL, "down momentum decelerating sharply")
        return FilterResult("momentum_exhaustion", FilterVerdict.PASS, "momentum not exhausted")

    def _sudden_reversal(self, f: FeatureSnapshot) -> FilterResult:
        r15, r60 = f.returns.get(15), f.returns.get(60)
        vol = f.realized_vol_60s
        if r15 is None or r60 is None or vol is None:
            return FilterResult("sudden_reversal", FilterVerdict.UNAVAILABLE, "missing 15s/60s returns")
        opposite = (r15 > 0 > r60) or (r15 < 0 < r60)
        threshold = max(0.0004, 2.5 * vol * math.sqrt(15))
        if opposite and abs(r15) >= threshold:
            return FilterResult("sudden_reversal", FilterVerdict.FAIL, f"15s reversal {r15:.5f}")
        return FilterResult("sudden_reversal", FilterVerdict.PASS, "no sudden reversal")

    def _volume_confirmation(self, f: FeatureSnapshot) -> FilterResult:
        if f.volume_180s <= 0 or f.tick_rate_30s <= 0:
            return FilterResult("volume_confirmation", FilterVerdict.UNAVAILABLE, "no trade-volume data")
        r15 = f.returns.get(15)
        if r15 is None:
            return FilterResult("volume_confirmation", FilterVerdict.UNAVAILABLE, "missing 15s return")
        expected_30 = float(f.volume_180s) / 6.0
        ratio = float(f.volume_30s) / expected_30 if expected_30 > 0 else 0.0
        if abs(r15) >= 0.0003 and ratio < 0.25:
            return FilterResult("volume_confirmation", FilterVerdict.FAIL, f"volume ratio {ratio:.2f}", score=ratio)
        return FilterResult("volume_confirmation", FilterVerdict.PASS, f"volume ratio {ratio:.2f}", score=ratio)

    def _oracle_divergence(self, f: FeatureSnapshot) -> FilterResult:
        if f.btc_price is None or f.twap_current is None or f.twap_current <= 0:
            return FilterResult("oracle_divergence", FilterVerdict.UNAVAILABLE, "missing oracle/spot price")
        basis_bps = abs(float(f.btc_price - f.twap_current) / float(f.twap_current) * 10000)
        if basis_bps > self.max_oracle_basis_bps:
            return FilterResult("oracle_divergence", FilterVerdict.FAIL, f"TWAP/spot basis {basis_bps:.1f} bps", score=basis_bps)
        return FilterResult("oracle_divergence", FilterVerdict.PASS, f"basis {basis_bps:.1f} bps", score=basis_bps)

    def _abnormal_order_book(self, f: FeatureSnapshot) -> FilterResult:
        if f.up_book is None or f.down_book is None:
            return FilterResult("abnormal_order_book", FilterVerdict.UNAVAILABLE, "one or both books missing")
        for book in (f.up_book, f.down_book):
            if book.best_bid is None or book.best_ask is None:
                return FilterResult("abnormal_order_book", FilterVerdict.UNAVAILABLE, "book side empty")
            if book.best_bid >= book.best_ask:
                return FilterResult("abnormal_order_book", FilterVerdict.FAIL, "crossed book")
            if book.spread is not None and book.spread > Decimal("0.20"):
                return FilterResult("abnormal_order_book", FilterVerdict.FAIL, f"implausible spread {book.spread}")
        return FilterResult("abnormal_order_book", FilterVerdict.PASS, "books normal")

    def _liquidity_withdrawal(self, f: FeatureSnapshot, required_shares: Decimal) -> FilterResult:
        if f.up_book is None and f.down_book is None:
            return FilterResult("liquidity_withdrawal", FilterVerdict.UNAVAILABLE, "no books")
        need = max(required_shares, Decimal("1")) * self.min_depth_multiple
        depths = [d for d in (f.up_depth, f.down_depth) if d > 0]
        if not depths:
            return FilterResult("liquidity_withdrawal", FilterVerdict.FAIL, "no visible depth")
        if max(depths) < need:
            return FilterResult("liquidity_withdrawal", FilterVerdict.FAIL, f"depth {max(depths):.2f} < required {need:.2f}")
        return FilterResult("liquidity_withdrawal", FilterVerdict.PASS, "depth present")

    def _spread_expansion(self, f: FeatureSnapshot) -> FilterResult:
        if f.up_spread is None or f.down_spread is None:
            return FilterResult("spread_expansion", FilterVerdict.UNAVAILABLE, "missing spread")
        worst = max(f.up_spread, f.down_spread)
        if worst > self.max_spread:
            return FilterResult("spread_expansion", FilterVerdict.FAIL, f"spread {worst} > {self.max_spread}")
        return FilterResult("spread_expansion", FilterVerdict.PASS, f"spread {worst}")
