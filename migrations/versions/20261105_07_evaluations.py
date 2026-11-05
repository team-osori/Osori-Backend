"""Durable A/B evaluations with frozen inputs and independently saved responses."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_07'
down_revision = '20261105_06'
branch_labels = None
depends_on = None


def upgrade():
    """Create evaluation queue and question result tables without altering existing inputs."""
    op.create_table('evaluations',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('service_id', sa.String(36), sa.ForeignKey('services.id'), nullable=False),
        sa.Column('idempotency_hash', sa.String(64), nullable=True),
        sa.Column('request_hash', sa.String(64), nullable=False), sa.Column('inputs', sa.JSON(), nullable=False),
        sa.Column('secret_ciphertext', sa.Text(), nullable=True), sa.Column('status', sa.String(30), nullable=False),
        sa.Column('total_questions', sa.Integer(), nullable=False), sa.Column('completed_questions', sa.Integer(), nullable=False),
        sa.Column('failed_questions', sa.Integer(), nullable=False), sa.Column('lease_token', sa.String(36), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('service_id', 'idempotency_hash', name='uq_evaluation_idempotency'))
    for column in ('service_id', 'status', 'created_at'):
        op.create_index('ix_evaluations_' + column, 'evaluations', [column])
    op.create_table('evaluation_items',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('evaluation_id', sa.String(36), sa.ForeignKey('evaluations.id'), nullable=False),
        sa.Column('question_id', sa.String(36), nullable=False), sa.Column('input', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False), sa.Column('a', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('b', sa.JSON(none_as_null=True), nullable=True), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('evaluation_id', 'question_id', name='uq_evaluation_question'))
    for column in ('evaluation_id', 'created_at'):
        op.create_index('ix_evaluation_items_' + column, 'evaluation_items', [column])


def downgrade():
    """Remove evaluation tables in reverse foreign-key order."""
    op.drop_table('evaluation_items')
    op.drop_table('evaluations')
