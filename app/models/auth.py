from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    __tablename__ = 'users'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    login_id: Mapped[str] = mapped_column(String(30).with_variant(String(30, collation='utf8mb4_bin'), 'mysql'), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(254).with_variant(String(254, collation='utf8mb4_bin'), 'mysql'), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(30))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    __tablename__ = 'auth_sessions'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsedRefreshToken(Base):
    __tablename__ = 'used_refresh_tokens'

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('auth_sessions.id', ondelete='CASCADE'), index=True)
