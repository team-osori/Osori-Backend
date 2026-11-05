from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Evaluation(Base):
    """Durable A/B job with immutable inputs and no plaintext authentication secrets."""
    __tablename__ = 'evaluations'
    __table_args__ = (UniqueConstraint('service_id', 'idempotency_hash', name='uq_evaluation_idempotency'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey('services.id'), index=True)
    idempotency_hash: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict] = mapped_column(JSON)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True)
    total_questions: Mapped[int] = mapped_column(Integer)
    completed_questions: Mapped[int] = mapped_column(Integer, default=0)
    failed_questions: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationItem(Base):
    """Persist each side separately so a partial call failure never erases the other response."""
    __tablename__ = 'evaluation_items'
    __table_args__ = (UniqueConstraint('evaluation_id', 'question_id', name='uq_evaluation_question'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey('evaluations.id'), index=True)
    question_id: Mapped[str] = mapped_column(String(36))
    input: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    a: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    b: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    review_revision: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EvaluationReview(Base):
    """Append-only human decisions retain automatic results and prior review history."""
    __tablename__ = 'evaluation_reviews'
    __table_args__ = (UniqueConstraint('item_id', 'revision', name='uq_evaluation_review_revision'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey('evaluation_items.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(1))
    condition_id: Mapped[str] = mapped_column(String(100))
    verdict: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(2000))
    reviewed_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EvaluationAnswerReview(Base):
    """Whole-answer O/X decisions never overwrite individual condition reviews."""
    __tablename__ = 'evaluation_answer_reviews'
    __table_args__ = (UniqueConstraint('item_id', 'revision', name='uq_evaluation_answer_review_revision'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey('evaluation_items.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(1))
    verdict: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(2000))
    reviewed_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
