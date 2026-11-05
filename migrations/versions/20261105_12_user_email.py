"""Add email identities without inventing addresses for legacy accounts."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_12'
down_revision = '20261105_25'
branch_labels = None
depends_on = None


def upgrade():
    """Add nullable email identities without modifying existing accounts."""
    email_type = sa.String(254).with_variant(sa.String(254, collation='utf8mb4_bin'), 'mysql')
    op.add_column('users', sa.Column('email', email_type, nullable=True))
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
    op.create_table('email_verifications',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('email', sa.String(254), nullable=False),
        sa.Column('code_hash', sa.String(64), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_email_verifications_expires_at', 'email_verifications', ['expires_at'])


def downgrade():
    """Remove the email column only when it contains no identities."""
    # Existing email identities must not be silently discarded on rollback.
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM users WHERE email IS NOT NULL')).scalar():
        raise RuntimeError('Cannot discard stored email identities; migrate them before downgrade')
    op.drop_table('email_verifications')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_column('users', 'email')
