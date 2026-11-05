"""Bound session lifetime and retained refresh history."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_04'
down_revision = '20261105_03'
branch_labels = None
depends_on = None


def upgrade():
    """Backfill existing sessions conservatively at their current refresh expiry."""
    op.add_column('auth_sessions', sa.Column('absolute_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('auth_sessions', sa.Column('refresh_count', sa.Integer(), server_default='0', nullable=False))
    op.execute('UPDATE auth_sessions SET absolute_expires_at = refresh_expires_at, '
               'refresh_count = (SELECT COUNT(*) FROM used_refresh_tokens '
               'WHERE used_refresh_tokens.session_id = auth_sessions.id)')
    with op.batch_alter_table('auth_sessions') as batch:
        batch.alter_column('absolute_expires_at', existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade():
    """Remove the additional session lifecycle fields."""
    with op.batch_alter_table('auth_sessions') as batch:
        batch.drop_column('refresh_count')
        batch.drop_column('absolute_expires_at')
