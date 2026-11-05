"""Append-only human condition decisions with optimistic concurrency."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_09'
down_revision = '20261105_08'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('evaluation_items', sa.Column('review_revision', sa.Integer(), nullable=False, server_default='0'))
    op.create_table('evaluation_reviews',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('item_id', sa.String(36), sa.ForeignKey('evaluation_items.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('side', sa.String(1), nullable=False),
        sa.Column('condition_id', sa.String(100), nullable=False),
        sa.Column('verdict', sa.String(20), nullable=False),
        sa.Column('reason', sa.String(2000), nullable=False),
        sa.Column('reviewed_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('item_id', 'revision', name='uq_evaluation_review_revision'))
    for column in ('item_id', 'created_at'):
        op.create_index('ix_evaluation_reviews_' + column, 'evaluation_reviews', [column])


def downgrade():
    op.drop_table('evaluation_reviews')
    op.drop_column('evaluation_items', 'review_revision')
