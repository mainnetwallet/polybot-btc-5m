"""Persist recent oracle TWAP observations.

Revision ID: 0002_twap_observations
Revises: 0001_initial
Create Date: 2026-11-07

Databases created by 0001 before this table existed get it here; databases created from the
current models already have it, hence ``checkfirst``.
"""
from __future__ import annotations

from alembic import op

from btc5m_bot.db.models import TwapObservationRow

revision = "0002_twap_observations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    TwapObservationRow.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    TwapObservationRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
