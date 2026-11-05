"""Create users, authentication sessions and rate-limit windows."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_01'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """Apply this revision's schema changes in dependency order."""
    login_id = sa.String(30).with_variant(sa.String(30, collation='utf8mb4_bin'), 'mysql')
    op.create_table(
        'users', sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('login_id', login_id, nullable=False),
        sa.Column('name', sa.String(30), nullable=False),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_users_login_id', 'users', ['login_id'], unique=True)
    op.create_table(
        'auth_sessions', sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('access_hash', sa.String(64), nullable=False),
        sa.Column('refresh_hash', sa.String(64), nullable=False),
        sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('refresh_expires_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_auth_sessions_user_id', 'auth_sessions', ['user_id'])
    op.create_index('ix_auth_sessions_access_hash', 'auth_sessions', ['access_hash'], unique=True)
    op.create_index('ix_auth_sessions_refresh_hash', 'auth_sessions', ['refresh_hash'], unique=True)
    op.create_table(
        'auth_rate_windows', sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('window_start', sa.BigInteger(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
    )
    op.create_index('ix_auth_rate_windows_window_start', 'auth_rate_windows', ['window_start'])


def downgrade():
    """Revert this revision's schema changes in reverse dependency order."""
    op.drop_table('auth_rate_windows')
    op.drop_table('auth_sessions')
    op.drop_table('users')
