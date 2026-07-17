from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260707_01"
down_revision = "20260624_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("model_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column(
                "mcp_server_list",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )

    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mcp_server_name", sa.String(length=255), nullable=False),
        sa.Column("mcp_server_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("agent_skills") as batch_op:
        batch_op.add_column(sa.Column("relative_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("metadata", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("skill_source", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("skill_md_url", sa.String(length=255), nullable=True))
        batch_op.drop_constraint("ck_agent_skills_source_type", type_="check")
        batch_op.drop_constraint("ck_agent_skills_repo_id_by_source", type_="check")
        batch_op.create_check_constraint(
            "ck_agent_skills_source_type",
            "source_type IN ('builtin', 'git', 'local', 'clawhub', 'wittyhub')",
        )
        batch_op.create_check_constraint(
            "ck_agent_skills_repo_id_by_source",
            "(source_type IN ('git', 'local', 'clawhub') AND repo_id IS NOT NULL) OR "
            "(source_type IN ('builtin', 'wittyhub') AND repo_id IS NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_skills") as batch_op:
        batch_op.drop_constraint("ck_agent_skills_source_type", type_="check")
        batch_op.drop_constraint("ck_agent_skills_repo_id_by_source", type_="check")
        batch_op.drop_column("skill_md_url")
        batch_op.drop_column("skill_source")
        batch_op.drop_column("metadata")
        batch_op.drop_column("relative_path")
        batch_op.create_check_constraint(
            "ck_agent_skills_source_type",
            "source_type IN ('builtin', 'git', 'local')",
        )
        batch_op.create_check_constraint(
            "ck_agent_skills_repo_id_by_source",
            "(source_type = 'builtin' AND repo_id IS NULL) OR "
            "(source_type IN ('git', 'local') AND repo_id IS NOT NULL)",
        )

    op.drop_table("mcp_servers")

    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("mcp_server_list")
        batch_op.drop_column("model_id")
