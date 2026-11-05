"""Remove retired email authentication data while preserving account identities."""
from alembic import op
import sqlalchemy as sa

revision = '20261106_39'
down_revision = '20261105_32'
branch_labels = None
depends_on = None


def upgrade():
    """Keep user IDs, login IDs, passwords, sessions and project ownership intact."""
    op.drop_table('email_verifications')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_column('users', 'email')


def downgrade():
    """Restore the old schema only; deleted addresses and challenges are not recoverable."""
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
