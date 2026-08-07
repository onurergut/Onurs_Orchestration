from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from orchestration.storage.models.project import Base


class LLMConfig(Base):
    __tablename__ = "llm_configs"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    endpoints: Mapped[list] = mapped_column(JSONB, nullable=False)
    tier_map: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fallback_chain: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    rate_limit: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())