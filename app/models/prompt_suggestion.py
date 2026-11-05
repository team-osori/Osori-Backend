"""Persist immutable suggestion evidence and atomically tracked selection/application."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PromptSuggestion(Base):
    __tablename__ = 'prompt_suggestions'
    __table_args__ = (UniqueConstraint('service_id', 'idempotency_hash', name='uq_prompt_suggestion_request'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey('services.id', ondelete='CASCADE'), index=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey('evaluations.id', ondelete='CASCADE'), index=True)
    base_version_id: Mapped[str] = mapped_column(ForeignKey('prompt_versions.id'))
    applied_version_id: Mapped[str | None] = mapped_column(ForeignKey('prompt_versions.id'), nullable=True)
    idempotency_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    application_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    suggestions: Mapped[list] = mapped_column(JSON)
    selected_ids: Mapped[list] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
