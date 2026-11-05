"""Baseline schema for POLYBOT.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-05

The baseline intentionally materialises the declarative metadata so that the
migration can never drift from the runtime models. Alembic still records the
revision and owns subsequent upgrades.
"""
from __future__ import annotations

from alembic import op

from btc5m_bot.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
