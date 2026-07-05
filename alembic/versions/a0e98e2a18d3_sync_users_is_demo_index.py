"""Add the users.is_demo index (first real Alembic migration).

Trimmed from autogenerate: the raw autogen also emitted ~18 alter_column
`nullable=False` ops -- a pre-existing gap where the models declare NOT NULL but
the hand-rolled schema left the columns nullable (with server defaults, so new
rows are non-null). Tightening them on adoption is risky (would fail on any
stray NULL) and is NOT what adopting Alembic is for, so they are intentionally
dropped here. Future autogenerate will re-surface them; reconcile separately
(backfill + enforce, or relax the model annotations) -- tracked, not blocking.

The one genuine, safe schema change is the missing index below.

Revision ID: a0e98e2a18d3
Revises: 67c4a50e3f19
Create Date: 2026-07-05 19:48:01.503788
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a0e98e2a18d3"
down_revision: Union[str, None] = "67c4a50e3f19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(op.f("ix_users_is_demo"), "users", ["is_demo"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_is_demo"), table_name="users")
