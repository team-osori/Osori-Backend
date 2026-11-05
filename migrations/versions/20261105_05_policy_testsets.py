"""Store immutable policy versions and human-confirmed test sets."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

revision = '20261105_05'
down_revision = '20261105_04'
branch_labels = None
depends_on = None


def upgrade():
    """Create source, draft, review and snapshot tables without changing auth or chatbot tables."""
    op.create_table('policies',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('name', sa.String(100), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('deleted', sa.Boolean(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_policies_project_id', 'policies', ['project_id'])
    op.create_table('policy_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('policy_id', sa.String(36), sa.ForeignKey('policies.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text().with_variant(MEDIUMTEXT(), 'mysql'), nullable=False),
        sa.Column('rules', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('policy_id', 'version', name='uq_policy_version'))
    op.create_index('ix_policy_versions_policy_id', 'policy_versions', ['policy_id'])
    op.create_table('testsets',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('name', sa.String(100), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_testsets_project_id', 'testsets', ['project_id'])
    op.create_table('testset_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('testset_id', sa.String(36), sa.ForeignKey('testsets.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False), sa.Column('policy_version_ids', sa.JSON(), nullable=False),
        sa.Column('snapshot', sa.JSON(none_as_null=True), nullable=True),
        sa.Column('confirmed_by', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('testset_id', 'version', name='uq_testset_version'))
    op.create_index('ix_testset_versions_testset_id', 'testset_versions', ['testset_id'])
    op.create_table('test_questions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('testset_version_id', sa.String(36), sa.ForeignKey('testset_versions.id'), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False), sa.Column('review_status', sa.String(20), nullable=False),
        sa.Column('reviewed_by', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('review_note', sa.String(2000), nullable=True), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_test_questions_testset_version_id', 'test_questions', ['testset_version_id'])
    op.create_table('question_generation_requests',
        sa.Column('key', sa.String(64), primary_key=True), sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False), sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    """Remove only this feature's tables in reverse foreign-key order."""
    for table in ('question_generation_requests', 'test_questions', 'testset_versions', 'testsets', 'policy_versions', 'policies'):
        op.drop_table(table)
