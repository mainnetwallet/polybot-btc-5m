"""Resolution-rule parsing, fingerprinting and confidence assessment."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..types import Methodology

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def methodology_from_metadata(market: dict[str, Any], event_start: datetime | None = None) -> Methodology:
    """Determine settlement methodology from live metadata, then rule text, then history.

    Metadata wins. Historical date buckets are only used when the market text does not
    specify a methodology; different methodologies must never be pooled.
    """
    cfg = market.get("cryptoMarketConfig") or {}
    if isinstance(cfg, dict) and cfg.get("twapEnabled"):
        lookback = cfg.get("twapLookbackSeconds")
        if lookback == 60:
            return Methodology.TWAP_60S
        if lookback == 30:
            return Methodology.TWAP_30S
        return Methodology.UNKNOWN

    source = _norm(str(market.get("resolutionSource") or "")).lower()
    description = _norm(str(market.get("description") or "")).lower()
    combined = f"{source} {description}"

    if "twap-60s" in combined or "60-second twap" in combined or "60 second twap" in combined:
        return Methodology.TWAP_60S
    if "twap-30s" in combined or "30-second twap" in combined or "30 second twap" in combined:
        return Methodology.TWAP_30S
    if "twap" in combined:
        return Methodology.UNKNOWN
    # Known historical transitions are context, not proof: only apply them when the
    # market is demonstrably Chainlink-sourced. Anything else stays UNKNOWN.
    if event_start is not None and ("chain.link" in combined or "chainlink" in combined):
        if event_start.date().isoformat() < "2026-08-07":
            return Methodology.SNAPSHOT
        if event_start.date().isoformat() <= "2026-08-13":
            return Methodology.TWAP_30S
        return Methodology.TWAP_60S
    return Methodology.UNKNOWN


@dataclass(frozen=True)
class ResolutionRuleSpec:
    fingerprint: str
    methodology: Methodology
    source: str
    tie_rule: str
    precision: str
    rounding: str
    window_seconds: int
    text: str
    confidence: str
    warnings: tuple[str, ...] = field(default_factory=tuple)
    source_url: str = ""

    def to_db(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "methodology": self.methodology.value,
            "source": self.source,
            "tie_rule": self.tie_rule,
            "precision": -1 if self.precision == "UNKNOWN" else int(self.precision),
            "rounding": self.rounding,
            "window_seconds": self.window_seconds,
            "text": self.text,
            "approved": False,
        }


def build_rule_spec(market: dict[str, Any], event_start: datetime, version: str) -> ResolutionRuleSpec:
    description = _norm(str(market.get("description") or ""))
    source = _norm(str(market.get("resolutionSource") or ""))
    combined = f"{description} {source}".lower()
    warnings: list[str] = []

    methodology = methodology_from_metadata(market, event_start)
    if methodology is Methodology.UNKNOWN:
        warnings.append("resolution methodology not recognised")

    source_lower = source.lower()
    if "chain.link" in source_lower or "chainlink" in combined:
        source_kind = "CHAINLINK"
    elif "pyth" in combined:
        source_kind = "PYTH"
        warnings.append("Pyth source detected; not a valid substitute for Chainlink")
    elif not source:
        source_kind = "UNKNOWN"
        warnings.append("resolution source missing")
    else:
        source_kind = "OTHER"
        warnings.append(f"unrecognised resolution source: {source[:80]}")

    if "greater than or equal" in combined:
        tie_rule = "UP"
    elif re.search(r"greater than(?!\s+or\s+equal)", combined):
        tie_rule = "DOWN"
    else:
        tie_rule = "UNKNOWN"
        warnings.append("tie rule not determined")

    precision = "UNKNOWN"
    m = re.search(r"(\d+)\s*(?:decimal places|decimals)", combined)
    if m:
        precision = m.group(1)
    if precision == "UNKNOWN":
        warnings.append("precision/rounding not documented")

    rounding = "UNKNOWN"
    if "rounded" in combined:
        rounding = "DOCUMENTED"
    else:
        warnings.append("rounding rule not documented")

    window = methodology.window_seconds
    canonical = json.dumps(
        {
            "methodology": methodology.value,
            "source": source_kind,
            "source_url": source,
            "tie": tie_rule,
            "precision": precision,
            "rounding": rounding,
            "description": description,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    if methodology is Methodology.UNKNOWN or source_kind != "CHAINLINK" or tie_rule == "UNKNOWN":
        confidence = "LOW"
    elif precision == "UNKNOWN" or rounding == "UNKNOWN":
        confidence = "MEDIUM"
    else:
        confidence = "HIGH"

    return ResolutionRuleSpec(
        fingerprint=fingerprint,
        methodology=methodology,
        source=source_kind,
        tie_rule=tie_rule,
        precision=precision,
        rounding=rounding,
        window_seconds=window,
        text=description,
        confidence=confidence,
        warnings=tuple(warnings),
        source_url=source,
    )
