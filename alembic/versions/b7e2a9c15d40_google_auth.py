"""google sign-in: remember which Google account a user is

Purely additive. Two nullable columns and one unique index - PostgreSQL adds a
nullable column without rewriting the table, so this runs instantly and touches
no existing row.

Revision ID: b7e2a9c15d40
Revises: a1f4c2d70b91
Create Date: 2026-08-24
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e2a9c15d40"
down_revision: Union[str, None] = "a1f4c2d70b91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(length=64), nullable=True))
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)
    op.add_column(
        "users", sa.Column("auth_provider", sa.String(length=24), nullable=True)
    )
    op.execute("UPDATE users SET auth_provider = 'password' WHERE auth_provider IS NULL")


def downgrade() -> None:
    op.drop_column("users", "auth_provider")
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "google_sub")
