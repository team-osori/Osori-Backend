"""connection and prompt management

Revision ID: 20261105_05
Revises: 20261105_04
Create Date: 2026-11-05 10:45:04.107602
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = '20261105_05'
down_revision = '20261105_04'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('chatbot_connections',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('service_id', sa.String(length=36), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('config', sa.JSON(), nullable=False),
    sa.Column('secret_ciphertext', sa.Text(), nullable=True),
    sa.Column('verified_revision', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('service_id')
    )
    op.create_table('prompts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('service_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('current_version_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_prompts_created_at'), 'prompts', ['created_at'], unique=False)
    op.create_index(op.f('ix_prompts_service_id'), 'prompts', ['service_id'], unique=False)
    op.create_table('prompt_versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('prompt_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('change_note', sa.Text(), nullable=True),
    sa.Column('external_version_ref', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['prompt_id'], ['prompts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('prompt_id', 'version', name='uq_prompt_version')
    )
    op.create_index(op.f('ix_prompt_versions_prompt_id'), 'prompt_versions', ['prompt_id'], unique=False)
    op.create_table('connection_tests',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('service_id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=36), nullable=False),
    sa.Column('connection_revision', sa.Integer(), nullable=False),
    sa.Column('idempotency_hash', sa.String(length=64), nullable=True),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('prompt_version_id', sa.String(length=36), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('answer', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=True),
    sa.Column('error', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('applied_version_verified', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['connection_id'], ['chatbot_connections.id'], ),
    sa.ForeignKeyConstraint(['prompt_version_id'], ['prompt_versions.id'], ),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('service_id', 'idempotency_hash', name='uq_connection_test_idempotency')
    )
    op.create_index(op.f('ix_connection_tests_created_at'), 'connection_tests', ['created_at'], unique=False)
    op.create_index(op.f('ix_connection_tests_service_id'), 'connection_tests', ['service_id'], unique=False)
    op.create_index(op.f('ix_connection_tests_status'), 'connection_tests', ['status'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_connection_tests_status'), table_name='connection_tests')
    op.drop_index(op.f('ix_connection_tests_service_id'), table_name='connection_tests')
    op.drop_index(op.f('ix_connection_tests_created_at'), table_name='connection_tests')
    op.drop_table('connection_tests')
    op.drop_index(op.f('ix_prompt_versions_prompt_id'), table_name='prompt_versions')
    op.drop_table('prompt_versions')
    op.drop_index(op.f('ix_prompts_service_id'), table_name='prompts')
    op.drop_index(op.f('ix_prompts_created_at'), table_name='prompts')
    op.drop_table('prompts')
    op.drop_table('chatbot_connections')
