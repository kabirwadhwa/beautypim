"""add reference knowledge corpus

Revision ID: d4a91e72bc10
Revises: c3f2a18d9b41
"""
from alembic import op
import sqlalchemy as sa

from app.database import GUID, PortableJSON

revision = "d4a91e72bc10"
down_revision = "c3f2a18d9b41"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_corpus_import_jobs",
        sa.Column("id", GUID(), primary_key=True), sa.Column("dataset_key", sa.String(100), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False), sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False), sa.Column("adapter_name", sa.String(100), nullable=False),
        sa.Column("adapter_version", sa.String(50), nullable=False), sa.Column("status", sa.String(50), nullable=False),
        *[sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in (
            "total_rows", "processed_rows", "imported_rows", "skipped_rows", "duplicate_rows", "failed_rows",
            "products_created", "variants_created", "observations_created", "formulations_created",
            "market_observations_created", "conflicts_detected")],
        sa.Column("metrics", PortableJSON()), sa.Column("error_summary", sa.Text()),
        sa.Column("requested_by_id", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('queued','processing','partially_completed','completed','failed','cancelled')", name="check_knowledge_import_status"),
        sa.UniqueConstraint("dataset_key", "file_hash", "adapter_version", name="uq_knowledge_import_file_adapter"))
    for column in ("dataset_key", "file_hash", "status", "heartbeat_at"):
        op.create_index(f"ix_knowledge_corpus_import_jobs_{column}", "knowledge_corpus_import_jobs", [column])

    op.create_table("knowledge_products",
        sa.Column("id", GUID(), primary_key=True), sa.Column("brand_name", sa.String(255), nullable=False),
        sa.Column("normalized_brand", sa.String(255), nullable=False), sa.Column("product_name", sa.String(500), nullable=False),
        sa.Column("normalized_name", sa.String(500), nullable=False), sa.Column("category", sa.String(255)),
        sa.Column("subcategory", sa.String(255)), sa.Column("product_type", sa.String(255)),
        sa.Column("application_area", sa.String(255)), sa.Column("identity_key", sa.String(64), nullable=False, unique=True),
        sa.Column("searchable_text", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for column in ("normalized_brand", "normalized_name", "category", "subcategory", "product_type", "identity_key"):
        op.create_index(f"ix_knowledge_products_{column}", "knowledge_products", [column])
    op.create_index("idx_knowledge_product_brand_name", "knowledge_products", ["normalized_brand", "normalized_name"])
    op.create_index("idx_knowledge_product_classification", "knowledge_products", ["category", "product_type"])

    op.create_table("knowledge_variants",
        sa.Column("id", GUID(), primary_key=True), sa.Column("knowledge_product_id", GUID(), sa.ForeignKey("knowledge_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("normalized_gtin", sa.String(50)), sa.Column("source_product_name", sa.String(500)),
        sa.Column("normalized_product_name", sa.String(500)), sa.Column("variant_name", sa.String(500)),
        sa.Column("normalized_variant", sa.String(500)), sa.Column("size_value", sa.String(100)),
        sa.Column("size_unit", sa.String(30)), sa.Column("shade", sa.String(255)), sa.Column("colour", sa.String(255)),
        sa.Column("undertone", sa.String(255)), sa.Column("identity_key", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for column in ("knowledge_product_id", "normalized_gtin", "normalized_product_name", "normalized_variant", "identity_key"):
        op.create_index(f"ix_knowledge_variants_{column}", "knowledge_variants", [column])
    op.create_index("idx_knowledge_variant_product_gtin", "knowledge_variants", ["knowledge_product_id", "normalized_gtin"])
    op.create_index("idx_knowledge_variant_product_name", "knowledge_variants", ["knowledge_product_id", "normalized_product_name"])
    op.create_index("idx_knowledge_variant_attributes", "knowledge_variants", ["knowledge_product_id", "normalized_variant", "size_value", "shade"])

    op.create_table("knowledge_source_observations",
        sa.Column("id", GUID(), primary_key=True), sa.Column("import_job_id", GUID(), sa.ForeignKey("knowledge_corpus_import_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_product_id", GUID(), sa.ForeignKey("knowledge_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_variant_id", GUID(), sa.ForeignKey("knowledge_variants.id", ondelete="SET NULL")),
        sa.Column("dataset_key", sa.String(100), nullable=False), sa.Column("source_sheet", sa.String(255), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False), sa.Column("source_record_id", sa.String(255)),
        sa.Column("source_parent_id", sa.String(255)), sa.Column("source_retailer", sa.String(255)), sa.Column("source_url", sa.Text()),
        sa.Column("locale", sa.String(20)), sa.Column("market", sa.String(50)), sa.Column("raw_payload", PortableJSON(), nullable=False),
        sa.Column("normalized_payload", PortableJSON(), nullable=False), sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("evidence_level", sa.String(30), nullable=False), sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("observation_date_type", sa.String(40), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("evidence_level IN ('exact_product','product_family','variant')", name="check_knowledge_observation_level"),
        sa.UniqueConstraint("import_job_id", "source_sheet", "source_row_number", name="uq_knowledge_source_row"))
    for column in ("import_job_id", "knowledge_product_id", "knowledge_variant_id", "dataset_key", "source_record_id", "source_parent_id", "source_hash"):
        op.create_index(f"ix_knowledge_source_observations_{column}", "knowledge_source_observations", [column])
    op.create_index("idx_knowledge_source_identity", "knowledge_source_observations", ["dataset_key", "source_record_id", "source_parent_id"])

    op.create_table("knowledge_field_observations",
        sa.Column("id", GUID(), primary_key=True), sa.Column("source_observation_id", GUID(), sa.ForeignKey("knowledge_source_observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_product_id", GUID(), sa.ForeignKey("knowledge_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_variant_id", GUID(), sa.ForeignKey("knowledge_variants.id", ondelete="CASCADE")),
        sa.Column("field_name", sa.String(100), nullable=False), sa.Column("raw_value", PortableJSON()),
        sa.Column("normalized_value", PortableJSON()), sa.Column("evidence_scope", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Numeric(5,4)), sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("evidence_scope IN ('exact_product','family','variant')", name="check_knowledge_field_scope"))
    for column in ("source_observation_id", "knowledge_product_id", "knowledge_variant_id", "field_name"):
        op.create_index(f"ix_knowledge_field_observations_{column}", "knowledge_field_observations", [column])
    op.create_index("idx_knowledge_field_product_name", "knowledge_field_observations", ["knowledge_product_id", "field_name"])
    op.create_index("idx_knowledge_field_variant_name", "knowledge_field_observations", ["knowledge_variant_id", "field_name"])

    op.create_table("knowledge_formulations",
        sa.Column("id", GUID(), primary_key=True), sa.Column("source_observation_id", GUID(), sa.ForeignKey("knowledge_source_observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_product_id", GUID(), sa.ForeignKey("knowledge_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_variant_id", GUID(), sa.ForeignKey("knowledge_variants.id", ondelete="CASCADE")),
        sa.Column("raw_inci_text", sa.Text(), nullable=False), sa.Column("normalized_ingredients", PortableJSON(), nullable=False),
        sa.Column("formulation_hash", sa.String(64), nullable=False), sa.Column("language", sa.String(20)), sa.Column("market", sa.String(50)),
        sa.Column("observed_at", sa.DateTime(timezone=True)), sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_observation_id", "formulation_hash", name="uq_knowledge_formulation_observation_hash"))
    for column in ("source_observation_id", "knowledge_product_id", "knowledge_variant_id", "formulation_hash"):
        op.create_index(f"ix_knowledge_formulations_{column}", "knowledge_formulations", [column])

    op.create_table("knowledge_market_observations",
        sa.Column("id", GUID(), primary_key=True), sa.Column("source_observation_id", GUID(), sa.ForeignKey("knowledge_source_observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_variant_id", GUID(), sa.ForeignKey("knowledge_variants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_retailer", sa.String(255)), sa.Column("market", sa.String(50)), sa.Column("currency", sa.String(10)),
        sa.Column("price", sa.Numeric(12,4)), sa.Column("original_price", sa.Numeric(12,4)), sa.Column("availability", sa.String(100)),
        sa.Column("image_url", sa.Text()), sa.Column("source_url", sa.Text()), sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("observation_date_type", sa.String(40), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for column in ("source_observation_id", "knowledge_variant_id"):
        op.create_index(f"ix_knowledge_market_observations_{column}", "knowledge_market_observations", [column])

    op.create_table("knowledge_conflicts",
        sa.Column("id", GUID(), primary_key=True), sa.Column("knowledge_product_id", GUID(), sa.ForeignKey("knowledge_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_variant_id", GUID(), sa.ForeignKey("knowledge_variants.id", ondelete="CASCADE")),
        sa.Column("field_name", sa.String(100), nullable=False), sa.Column("conflict_type", sa.String(50), nullable=False),
        sa.Column("values", PortableJSON(), nullable=False), sa.Column("source_observation_ids", PortableJSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('open','accepted','dismissed')", name="check_knowledge_conflict_status"))
    for column in ("knowledge_product_id", "knowledge_variant_id", "field_name", "status"):
        op.create_index(f"ix_knowledge_conflicts_{column}", "knowledge_conflicts", [column])
    op.create_index("idx_knowledge_conflict_target", "knowledge_conflicts", ["knowledge_product_id", "knowledge_variant_id", "field_name", "status"])


def downgrade():
    for table in ("knowledge_conflicts", "knowledge_market_observations", "knowledge_formulations", "knowledge_field_observations", "knowledge_source_observations", "knowledge_variants", "knowledge_products", "knowledge_corpus_import_jobs"):
        op.drop_table(table)
