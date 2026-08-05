"""add workflow_state to investigations

Adds the JSONB column the cyclic (Step 6) investigation graph writes at
every node transition: which node last ran, how many retries have been
spent, which agents are scheduled/have already run, and what the
validation step could not confirm. Without it, an investigation
interrupted mid-loop could recover its evidence and hypotheses but not
its position in the loop.

Revision ID: 8b1c74e0a2d9
Revises: 413f58ed95fc
Create Date: 2026-08-05 12:14:03.118427

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8b1c74e0a2d9'
down_revision: Union[str, Sequence[str], None] = '413f58ed95fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "investigations",
        sa.Column(
            "workflow_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("investigations", "workflow_state")
