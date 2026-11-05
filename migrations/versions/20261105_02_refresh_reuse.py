"""Keep used refresh token hashes for session replay detection."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_02'
down_revision = '20261105_01'
branch_labels = None
depends_on = None


def upgrade():
    """Apply this revision's schema changes in dependency order."""
    op.create_table(
        'used_refresh_tokens',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('auth_sessions.id', ondelete='CASCADE'), nullable=False),
    )
    op.create_index('ix_used_refresh_tokens_session_id', 'used_refresh_tokens', ['session_id'])


def downgrade():
    """Revert this revision's schema changes in reverse dependency order."""
    op.drop_table('used_refresh_tokens')
