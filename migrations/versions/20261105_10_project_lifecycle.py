"""Project trash, collaborative membership, last access and resource archival."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_10'
down_revision = '20261105_09'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('projects', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_projects_deleted_at', 'projects', ['deleted_at'])
    op.add_column('users', sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))
    for table in ('prompts', 'testsets'):
        op.add_column(table, sa.Column('deleted', sa.Boolean(), nullable=False, server_default='0'))
    op.create_table('project_members',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('invited_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('role', sa.String(10), nullable=False),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('project_id', 'user_id', name='uq_project_member'))
    for column in ('project_id', 'user_id'):
        op.create_index('ix_project_members_' + column, 'project_members', [column])


def downgrade():
    op.drop_table('project_members')
    for table in ('prompts', 'testsets'):
        op.drop_column(table, 'deleted')
    op.drop_column('users', 'last_seen_at')
    op.drop_index('ix_projects_deleted_at', table_name='projects')
    op.drop_column('projects', 'deleted_at')
