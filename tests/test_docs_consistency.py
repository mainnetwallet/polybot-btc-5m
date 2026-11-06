"""Docs must not drift from the code they describe."""
from __future__ import annotations

from pathlib import Path

from btc5m_bot.types import ReasonCode

DOCS = Path(__file__).resolve().parents[1] / "docs" / "OPERATIONS.md"


def _rows() -> list[str]:
    return [line for line in DOCS.read_text(encoding="utf-8").splitlines() if line.startswith("|")]


def test_every_reason_code_is_documented_in_operations_md():
    text = "\n".join(_rows())
    missing = [code.value for code in ReasonCode if f"`{code.value}`" not in text]
    assert not missing, f"ReasonCode values missing from docs/OPERATIONS.md: {missing}"


def test_edge_and_ev_reason_codes_have_separate_rows():
    # They mean different things: NEGATIVE_EV loses money in expectation, INSUFFICIENT_EDGE is
    # positive but below min_net_edge. A combined row hides that distinction.
    for row in _rows():
        assert not ("`INSUFFICIENT_EDGE`" in row and "`NEGATIVE_EV`" in row), row
