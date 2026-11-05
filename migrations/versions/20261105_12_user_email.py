"""Add email identities without inventing addresses for legacy accounts."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_12'
down_revision = '20261105_11'
branch_labels = None
depends_on = None


def upgrade():
    email_type = sa.String(254).with_variant(sa.String(254, collation='utf8mb4_bin'), 'mysql')
    op.add_column('users', sa.Column('email', email_type, nullable=True))
    op.create_index('ix_users_email', 'users', ['email'], unique=True)


def downgrade():
    # Existing email identities must not be silently discarded on rollback.
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM users WHERE email IS NOT NULL')).scalar():
        raise RuntimeError('Cannot discard stored email identities; migrate them before downgrade')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_column('users', 'email')
