"""SQLAlchemy model for the `investigations` table: the persisted state
of an AI investigation as it runs, so agents can create it, keep
appending evidence/hypotheses as they work, and resume it by id if the
process restarts mid-investigation."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class InvestigationStatus(str, enum.Enum):
    PENDING = "pending"
    INVESTIGATING = "investigating"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    RESOLVED = "resolved"


class Investigation(Base):
    """One row per investigation. `evidence` and `hypotheses` are JSONB
    lists that agents append to as they gather findings:

      evidence:   [{"source": ..., "finding": ..., "confidence": ...}, ...]
      hypotheses: [{"description": ..., "supporting_evidence": [...],
                     "confidence_score": ...}, ...]

    `workflow_state` holds the loop-control position of the LangGraph
    workflow (which node last ran, how many retries have been spent,
    which agents are scheduled//have run, what validation refuted). It's
    rewritten at every node transition so an investigation interrupted
    mid-loop can be resumed at the right pass rather than from scratch:

      workflow_state: {"current_node": ..., "retry_count": ...,
                       "agents_to_run": [...], "agents_completed": [...],
                       "validation_notes": [...]}
    """

    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    issue_description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[InvestigationStatus] = mapped_column(
        Enum(
            InvestigationStatus,
            name="investigation_status",
            native_enum=True,
            # Without this, SQLAlchemy persists the Python member *name*
            # (e.g. "PENDING") instead of its value ("pending"), which
            # doesn't match the lowercase values in the Postgres enum
            # type created by the Alembic migration.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=InvestigationStatus.PENDING,
        server_default=InvestigationStatus.PENDING.value,
    )
    evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    hypotheses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    workflow_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    final_root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return (
            f"Investigation(id={self.id!s}, status={self.status!s}, "
            f"issue_description={self.issue_description!r})"
        )
