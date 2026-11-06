"""Bounds that keep exit tunables from silently disabling a protection."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from btc5m_bot.config import RuntimeConfig


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # A binary share can lose at most 100%, so a -100% stop can never fire: it is "no stop".
        ("stop_loss_roi", "-1"),
        # Unreachable targets (price is capped at 1) silently turn take-profit off.
        ("take_profit_roi", "11"),
    ],
)
def test_values_that_disable_an_exit_are_rejected(field, value):
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stop_loss_roi", "-0.99"),
        ("stop_loss_roi", "-0.01"),
        ("take_profit_roi", "10"),
    ],
)
def test_boundary_values_still_accepted(field, value):
    RuntimeConfig.model_validate({field: value})
