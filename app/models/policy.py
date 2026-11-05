from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Policy(Base):
    """Project-scoped policy identity; historical versions survive logical deletion."""
    __tablename__ = 'policies'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyVersion(Base):
    """Immutable original policy text and human-authored compliance rules."""
    __tablename__ = 'policy_versions'
    __table_args__ = (UniqueConstraint('policy_id', 'version', name='uq_policy_version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey('policies.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), 'mysql'))
    rules: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TestSet(Base):
    """Named project-owned test set containing draft or confirmed versions."""
    __tablename__ = 'testsets'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TestSetVersion(Base):
    """Reviewable draft; confirmation freezes both policy evidence and questions."""
    __tablename__ = 'testset_versions'
    __table_args__ = (UniqueConstraint('testset_id', 'version', name='uq_testset_version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    testset_id: Mapped[str] = mapped_column(ForeignKey('testsets.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    policy_version_ids: Mapped[list] = mapped_column(JSON)
    snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    confirmed_by: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TestQuestion(Base):
    """Question with source evidence and explicit human review state."""
    __tablename__ = 'test_questions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    testset_version_id: Mapped[str] = mapped_column(ForeignKey('testset_versions.id'), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    review_status: Mapped[str] = mapped_column(String(20))
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    review_note: Mapped[str | None] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QuestionHistory(Base):
    """Append-only audit events, retained even when a draft question is deleted."""
    __tablename__ = 'question_history'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    testset_version_id: Mapped[str] = mapped_column(ForeignKey('testset_versions.id'), index=True)
    question_id: Mapped[str] = mapped_column(String(36), index=True)
    version_revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    note: Mapped[str | None] = mapped_column(String(2000))
    before: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    after: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GenerationRequest(Base):
    """Successful generation replay records; failures roll back for safe retry."""
    __tablename__ = 'question_generation_requests'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
