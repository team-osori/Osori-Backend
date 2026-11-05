"""Add review timestamps and durable question audit events without rewriting snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_08'
down_revision = '20261105_07'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('test_questions', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('question_history',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('testset_version_id', sa.String(36), sa.ForeignKey('testset_versions.id'), nullable=False),
        sa.Column('question_id', sa.String(36), nullable=False),
        sa.Column('version_revision', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('note', sa.String(2000), nullable=True),
        sa.Column('before', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('after', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    for column in ('testset_version_id', 'question_id'):
        op.create_index('ix_question_history_' + column, 'question_history', [column])


def downgrade():
    op.drop_table('question_history')
    op.drop_column('test_questions', 'reviewed_at')
