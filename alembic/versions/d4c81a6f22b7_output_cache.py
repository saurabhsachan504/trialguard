"""output cache: ek baar bani summary sabke liye

Purely additive - ek nayi table, kisi maujooda table ko chhua nahi gaya.

Revision ID: d4c81a6f22b7
Revises: b7e2a9c15d40
Create Date: 2026-09-03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4c81a6f22b7"
down_revision: Union[str, None] = "b7e2a9c15d40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cached_outputs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("lang", sa.String(length=8), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("detected_lang", sa.String(length=8), nullable=False),
        sa.Column("transcript_chars", sa.Integer(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "video_id", "mode", "lang", "model", "prompt_version",
            name="uq_cached_output_key",
        ),
    )
    op.create_index("ix_cached_outputs_last_used_at", "cached_outputs", ["last_used_at"])
    op.create_index("ix_cached_outputs_video_id", "cached_outputs", ["video_id"])


def downgrade() -> None:
    op.drop_index("ix_cached_outputs_video_id", table_name="cached_outputs")
    op.drop_index("ix_cached_outputs_last_used_at", table_name="cached_outputs")
    op.drop_table("cached_outputs")
