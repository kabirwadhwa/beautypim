"""repair production schema drift

Revision ID: 4c2a91d6f8be
Revises: 1be10a745cea
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4c2a91d6f8be"
down_revision: Union[str, None] = "1be10a745cea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    field_value_columns = {column["name"] for column in inspector.get_columns("field_values")}
    missing_field_value_columns = {
        "override_reason": sa.Text(),
        "evidence": sa.JSON(),
        "reasoning_summary": sa.Text(),
        "semantic_status": sa.String(length=100),
        "semantic_status_type": sa.String(length=100),
    }
    for name, column_type in missing_field_value_columns.items():
        if name not in field_value_columns:
            op.add_column("field_values", sa.Column(name, column_type, nullable=True))

    formulation_columns = {
        column["name"] for column in inspector.get_columns("formulation_ingredients")
    }
    if "evidence" not in formulation_columns:
        op.add_column("formulation_ingredients", sa.Column("evidence", sa.JSON(), nullable=True))
    if "key_ingredient_status" not in formulation_columns:
        op.add_column(
            "formulation_ingredients",
            sa.Column("key_ingredient_status", sa.String(length=100), nullable=True),
        )

    file_hash_index = next(
        (index for index in inspector.get_indexes("import_jobs") if index["name"] == "ix_import_jobs_file_hash"),
        None,
    )
    if file_hash_index and file_hash_index.get("unique"):
        op.drop_index("ix_import_jobs_file_hash", table_name="import_jobs")
        op.create_index("ix_import_jobs_file_hash", "import_jobs", ["file_hash"], unique=False)


def downgrade() -> None:
    # This repair intentionally has no destructive downgrade: production data may
    # already depend on the restored columns and multiple imports per file hash.
    pass
