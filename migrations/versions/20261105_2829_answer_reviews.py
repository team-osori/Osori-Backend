"""Store whole-answer O/X independently from condition-level review history."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_2829'
down_revision = '20261105_25'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('evaluation_answer_reviews',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('item_id', sa.String(36), sa.ForeignKey('evaluation_items.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('side', sa.String(1), nullable=False),
        sa.Column('verdict', sa.String(20), nullable=False),
        sa.Column('reason', sa.String(2000), nullable=True),
        sa.Column('reviewed_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('item_id', 'revision', name='uq_evaluation_answer_review_revision'))
    for column in ('item_id', 'created_at'):
        op.create_index('ix_evaluation_answer_reviews_' + column, 'evaluation_answer_reviews', [column])


def downgrade():
    op.drop_table('evaluation_answer_reviews')
