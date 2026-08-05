"""create investigations table

Revision ID: 413f58ed95fc
Revises: 
Create Date: 2026-08-05 00:43:12.550885

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '413f58ed95fc'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INVESTIGATION_STATUS_ENUM = postgresql.ENUM(
    "pending",
    "investigating",
    "needs_human_review",
    "resolved",
    name="investigation_status",
)


def upgrade() -> None:
    """Upgrade schema."""
    # op.create_table below creates the investigation_status enum type
    # automatically as part of creating the column that uses it.
    op.create_table(
        "investigations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("issue_description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            INVESTIGATION_STATUS_ENUM,
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "hypotheses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("final_root_cause", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("investigations")
    INVESTIGATION_STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
