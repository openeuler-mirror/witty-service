"""add skill repo tables

Revision ID: 005
Revises: 004
Create Date: 2026-03-26 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('skill_repo'):
        op.create_table(
            'skill_repo',
            sa.Column('repo_id', sa.String(), nullable=False),
            sa.Column('user_id', sa.String(), nullable=False),
            sa.Column('name', sa.String(), nullable=False),
            sa.Column('source_type', sa.String(), nullable=False),
            sa.Column('branch', sa.String(), nullable=True),
            sa.Column('url', sa.String(), nullable=True),
            sa.Column('local_path', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('repo_id'),
            sa.UniqueConstraint('user_id', 'name', name='uq_skill_repo_user_name'),
        )
        op.create_index(
            op.f('ix_skill_repo_user_id'),
            'skill_repo',
            ['user_id'],
            unique=False,
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table('skill_repo_discovery_cache'):
        op.create_table(
            'skill_repo_discovery_cache',
            sa.Column('user_id', sa.String(), nullable=False),
            sa.Column('repo_id', sa.String(), nullable=False),
            sa.Column('repo_name', sa.String(), nullable=False),
            sa.Column('discover_status', sa.String(), nullable=False),
            sa.Column(
                'skill_num',
                sa.Integer(),
                nullable=False,
                server_default='0',
            ),
            sa.Column('discovered_skills', sa.JSON(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('user_id', 'repo_id'),
            sa.UniqueConstraint(
                'user_id', 'repo_id', name='uq_skill_repo_cache_user_repo'
            ),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('skill_repo_discovery_cache'):
        op.drop_table('skill_repo_discovery_cache')

    inspector = sa.inspect(bind)
    if inspector.has_table('skill_repo'):
        indexes = {index['name'] for index in inspector.get_indexes('skill_repo')}
        ix_name = op.f('ix_skill_repo_user_id')
        if ix_name in indexes:
            op.drop_index(ix_name, table_name='skill_repo')
        op.drop_table('skill_repo')
