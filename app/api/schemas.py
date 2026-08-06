"""Pydantic request/response models for the investigations HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class InvestigationCreate(BaseModel):
    issue_description: str = Field(..., min_length=1)


class InvestigationCreateResponse(BaseModel):
    id: UUID
    status: str


class InvestigationSummary(BaseModel):
    id: UUID
    issue_description: str
    status: str
    final_root_cause: Optional[str]
    created_at: datetime
    updated_at: datetime


class InvestigationDetail(BaseModel):
    id: UUID
    issue_description: str
    status: str
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    final_root_cause: Optional[str]
    created_at: datetime
    updated_at: datetime
