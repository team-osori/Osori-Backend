from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Connection(Base):
    __tablename__ = 'chatbot_connections'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey('services.id'), unique=True)
    revision: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    verified_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectionTest(Base):
    __tablename__ = 'connection_tests'
    __table_args__ = (UniqueConstraint('service_id', 'idempotency_hash', name='uq_connection_test_idempotency'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey('services.id'), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey('chatbot_connections.id'))
    connection_revision: Mapped[int] = mapped_column(Integer)
    idempotency_hash: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    prompt_version_id: Mapped[str | None] = mapped_column(ForeignKey('prompt_versions.id'))
    status: Mapped[str] = mapped_column(String(20), index=True)
    answer: Mapped[str | None] = mapped_column(Text().with_variant(MEDIUMTEXT(), 'mysql'))
    error: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    applied_version_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
