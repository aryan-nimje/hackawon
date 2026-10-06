"""Tables. Whole pydantic objects go into a JSON(B) column; the few fields we filter on get real columns."""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# JSONB on PostgreSQL, plain JSON elsewhere (SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class RunRow(SQLModel, table=True):
    __tablename__ = "runs"

    id: str = Field(primary_key=True)
    status: str
    city: Optional[str] = None
    created_at: datetime = Field(index=True)
    updated_at: datetime
    is_active: bool = Field(default=False, index=True)
    data: Dict[str, Any] = Field(sa_column=Column(JSONType, nullable=False))  # full RunState


class IncidentRow(SQLModel, table=True):
    __tablename__ = "incidents"

    id: str = Field(primary_key=True)
    source: str
    created_at: datetime = Field(index=True)
    data: Dict[str, Any] = Field(sa_column=Column(JSONType, nullable=False))  # full Incident


class SubmissionRow(SQLModel, table=True):
    """Reports submitted through THIS backend's POST /reports (not the deployed citizen app's table)."""

    __tablename__ = "report_submissions"

    token: str = Field(primary_key=True)
    created_at: str  # ISO-8601 string, exactly as handed to the citizen
    run_id: Optional[str] = Field(default=None, index=True)
    submission: Dict[str, Any] = Field(sa_column=Column(JSONType, nullable=False))


class SignalRow(SQLModel, table=True):
    """External evidence (SACHET alerts now; news / weather later) in the common signal format."""

    __tablename__ = "signals"

    id: str = Field(primary_key=True)  # "<source>:<source id>"
    source: str = Field(index=True)
    kind: str
    status: str = Field(index=True)
    issued_at: Optional[datetime] = Field(default=None, index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)
    fetched_at: datetime
    data: Dict[str, Any] = Field(sa_column=Column(JSONType, nullable=False))  # full Signal
