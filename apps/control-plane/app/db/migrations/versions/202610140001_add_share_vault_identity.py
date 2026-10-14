"""add nullable vault_id / vault_name to shares (GH #265)

Expand-only: both columns are nullable, NULL = legacy share. Old code ignores them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "202610140001"
down_revision = "202609050001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shares", sa.Column("vault_id", sa.String(length=128), nullable=True))
    op.add_column("shares", sa.Column("vault_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("shares", "vault_name")
    op.drop_column("shares", "vault_id")
