"""Add ai_usage_events for per-job token and cost accounting

Creates the single table every paid AI call records into, so token
consumption and its estimated cost can be attributed to a product, an import
job, a research job or a bulk group.

``estimated_cost_usd`` is nullable on purpose: a model with no known rate
records its tokens with no cost rather than a misleading zero, and
``pricing_source`` says how any present figure was derived.

Revision ID: c7d2e4a81f55
Revises: 24a9b7e3c601
Create Date: 2026-11-09

"""
from alembic import op
import sqlalchemy as sa

from app.database import GUID, PortableJSON

revision = "c7d2e4a81f55"
down_revision = "24a9b7e3c601"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column("id", GUID(), primary_key=True, nullable=False),
        sa.Column("operation", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="success"),
        sa.Column("canonical_product_id", GUID(), nullable=True),
        sa.Column("product_variant_id", GUID(), nullable=True),
        sa.Column("import_job_id", GUID(), nullable=True),
        sa.Column("research_job_id", GUID(), nullable=True),
        sa.Column("enrichment_run_id", GUID(), nullable=True),
        sa.Column("bulk_group_id", GUID(), nullable=True),
        sa.Column("requested_by_id", GUID(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("web_search_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("pricing_source", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("processing_time_ms", sa.Integer(), nullable=True),
        sa.Column("detail", PortableJSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["canonical_product_id"], ["canonical_products.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["product_variant_id"], ["product_variants.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["import_job_id"], ["import_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["research_job_id"], ["crawl_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["enrichment_run_id"], ["enrichment_runs.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["requested_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "operation IN ('enrichment', 'web_research', 'review_summary', "
            "'catalog_assistant', 'other')",
            name="check_ai_usage_operation",
        ),
        sa.CheckConstraint(
            "pricing_source IN ('override', 'reference', 'calibrated', 'unknown')",
            name="check_ai_usage_pricing_source",
        ),
    )
    op.create_index("ix_ai_usage_events_operation", "ai_usage_events", ["operation"])
    op.create_index("ix_ai_usage_events_model", "ai_usage_events", ["model"])
    op.create_index(
        "ix_ai_usage_events_canonical_product_id", "ai_usage_events", ["canonical_product_id"],
    )
    op.create_index("ix_ai_usage_events_import_job_id", "ai_usage_events", ["import_job_id"])
    op.create_index("ix_ai_usage_events_research_job_id", "ai_usage_events", ["research_job_id"])
    op.create_index("ix_ai_usage_events_bulk_group_id", "ai_usage_events", ["bulk_group_id"])
    op.create_index("ix_ai_usage_events_requested_by_id", "ai_usage_events", ["requested_by_id"])
    op.create_index("ix_ai_usage_events_created_at", "ai_usage_events", ["created_at"])
    # Composite indexes for the aggregate queries the usage endpoints run.
    op.create_index(
        "idx_ai_usage_product_created", "ai_usage_events",
        ["canonical_product_id", "created_at"],
    )
    op.create_index(
        "idx_ai_usage_operation_created", "ai_usage_events", ["operation", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_ai_usage_operation_created", table_name="ai_usage_events")
    op.drop_index("idx_ai_usage_product_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_created_at", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_requested_by_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_bulk_group_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_research_job_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_import_job_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_canonical_product_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_model", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_operation", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")
