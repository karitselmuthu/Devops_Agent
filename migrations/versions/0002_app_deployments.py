"""Add application deployment plans and their job outbox.

Revision ID: 0002_app_deployments
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_app_deployments"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_deployments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "app_job_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("deployment_id", sa.String(36), sa.ForeignKey("app_deployments.id"), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("app_job_outbox")
    op.drop_table("app_deployments")
