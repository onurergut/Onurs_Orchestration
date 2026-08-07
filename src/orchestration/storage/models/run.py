from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from orchestration.storage.models.project import Base


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    task_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True)
    flow_uuid: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_stages: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    budget_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_runs_project_status", "project_id", "status"),)