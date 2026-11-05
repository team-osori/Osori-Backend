"""Retain project creation receipts for safe retries without duplicating resources."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_25'
down_revision = '20261105_10'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('projects') as batch:
        batch.add_column(sa.Column('creation_key_hash', sa.String(64), nullable=True))
        batch.add_column(sa.Column('creation_request_hash', sa.String(64), nullable=True))
        batch.create_unique_constraint('uq_projects_creation_key_hash', ['creation_key_hash'])


def downgrade():
    with op.batch_alter_table('projects') as batch:
        batch.drop_constraint('uq_projects_creation_key_hash', type_='unique')
        batch.drop_column('creation_request_hash')
        batch.drop_column('creation_key_hash')
