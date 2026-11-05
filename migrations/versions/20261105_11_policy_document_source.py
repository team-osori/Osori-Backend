"""Preserve reviewed document locations on immutable policy versions."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_11'
down_revision = '20261105_10'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('policy_versions', sa.Column('source_document', sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    op.drop_column('policy_versions', 'source_document')
