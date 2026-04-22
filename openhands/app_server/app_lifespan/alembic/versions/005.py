"""add skill repo tables and skill_install metadata columns

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
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('user_id', 'repo_id'),
            sa.UniqueConstraint(
                'user_id', 'repo_id', name='uq_skill_repo_cache_user_repo'
            ),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table('skill_install'):
        op.create_table(
            'skill_install',
            sa.Column('install_id', sa.String(), nullable=False),
            sa.Column('user_id', sa.String(), nullable=False),
            sa.Column('name', sa.String(), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('activation_type', sa.String(), nullable=False),
            sa.Column('triggers', sa.JSON(), nullable=False),
            sa.Column('target_scope', sa.String(), nullable=False),
            sa.Column('file_path', sa.String(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('install_id'),
            sa.UniqueConstraint('file_path'),
        )
        op.create_index(
            op.f('ix_skill_install_user_id'),
            'skill_install',
            ['user_id'],
            unique=False,
        )
        inspector = sa.inspect(bind)

    existing_columns = {
        column['name'] for column in inspector.get_columns('skill_install')
    }
    if 'source_type' not in existing_columns:
        op.add_column(
            'skill_install',
            sa.Column(
                'source_type', sa.String(), nullable=False, server_default='manual'
            ),
        )
    if 'installed_via' not in existing_columns:
        op.add_column(
            'skill_install',
            sa.Column(
                'installed_via', sa.String(), nullable=False, server_default='manual'
            ),
        )
    if 'catalog_key' not in existing_columns:
        op.add_column(
            'skill_install',
            sa.Column('catalog_key', sa.String(), nullable=True),
        )
    if 'source_repo' not in existing_columns:
        op.add_column(
            'skill_install',
            sa.Column('source_repo', sa.JSON(), nullable=True),
        )
    if 'source_ref' not in existing_columns:
        op.add_column(
            'skill_install',
            sa.Column('source_ref', sa.String(), nullable=True),
        )
    if 'readme_url' not in existing_columns:
        op.add_column(
            'skill_install',
            sa.Column('readme_url', sa.String(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('skill_repo_discovery_cache'):
        op.drop_table('skill_repo_discovery_cache')

    if inspector.has_table('skill_install'):
        existing_columns = {
            column['name'] for column in inspector.get_columns('skill_install')
        }
        if 'readme_url' in existing_columns:
            op.drop_column('skill_install', 'readme_url')
        if 'source_ref' in existing_columns:
            op.drop_column('skill_install', 'source_ref')
        if 'source_repo' in existing_columns:
            op.drop_column('skill_install', 'source_repo')
        if 'catalog_key' in existing_columns:
            op.drop_column('skill_install', 'catalog_key')
        if 'installed_via' in existing_columns:
            op.drop_column('skill_install', 'installed_via')
        if 'source_type' in existing_columns:
            op.drop_column('skill_install', 'source_type')

    inspector = sa.inspect(bind)
    if inspector.has_table('skill_repo'):
        indexes = {index['name'] for index in inspector.get_indexes('skill_repo')}
        ix_name = op.f('ix_skill_repo_user_id')
        if ix_name in indexes:
            op.drop_index(ix_name, table_name='skill_repo')
        op.drop_table('skill_repo')
