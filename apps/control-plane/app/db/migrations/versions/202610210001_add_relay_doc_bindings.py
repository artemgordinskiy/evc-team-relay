"""add relay_doc_bindings (doc_id -> share_id) for GHSA-9q47-g5jx-w24v

Expand-only: a new table, no change to existing ones. Old code ignores it.
No FK to shares on purpose: bindings outlive a deleted share.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202610210001"
down_revision = "202610140001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "relay_doc_bindings",
        sa.Column("doc_id", sa.String(length=512), primary_key=True),
        sa.Column(
            "share_id",
            postgresql.UUID(as_uuid=True).with_variant(sa.Uuid(), "sqlite"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_relay_doc_bindings_share_id", "relay_doc_bindings", ["share_id"])


def downgrade() -> None:
    op.drop_index("ix_relay_doc_bindings_share_id", table_name="relay_doc_bindings")
    op.drop_table("relay_doc_bindings")
