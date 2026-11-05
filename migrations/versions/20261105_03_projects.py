"""Create owner-scoped projects and one chatbot per project."""
from alembic import op
import sqlalchemy as sa

revision = '20261105_03'
down_revision = '20261105_02'
branch_labels = None
depends_on = None


def upgrade():
    """Apply this revision's schema changes in dependency order."""
    op.create_table('projects',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_projects_owner_id', 'projects', ['owner_id'])
    op.create_index('ix_projects_created_at', 'projects', ['created_at'])
    op.create_table('services',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_services_project_id', 'services', ['project_id'], unique=True)


def downgrade():
    """Revert this revision's schema changes in reverse dependency order."""
    op.drop_table('services')
    op.drop_table('projects')
