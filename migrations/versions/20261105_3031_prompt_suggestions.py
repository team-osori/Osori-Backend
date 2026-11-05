"""Isolated prompt improvement evidence, selection and application history (#30/31)."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_3031'
down_revision = '20261105_25'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('prompt_suggestions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('service_id', sa.String(36), sa.ForeignKey('services.id', ondelete='CASCADE'), nullable=False),
        sa.Column('evaluation_id', sa.String(36), sa.ForeignKey('evaluations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('base_version_id', sa.String(36), sa.ForeignKey('prompt_versions.id'), nullable=False),
        sa.Column('applied_version_id', sa.String(36), sa.ForeignKey('prompt_versions.id'), nullable=True),
        sa.Column('idempotency_hash', sa.String(64), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('application_hash', sa.String(64), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('suggestions', sa.JSON(), nullable=False),
        sa.Column('selected_ids', sa.JSON(), nullable=False),
        sa.Column('error', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('service_id', 'idempotency_hash', name='uq_prompt_suggestion_request'))
    for column in ('service_id', 'evaluation_id', 'created_at'):
        op.create_index('ix_prompt_suggestions_' + column, 'prompt_suggestions', [column])


def downgrade():
    op.drop_table('prompt_suggestions')
