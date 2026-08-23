"""add product tags and user display names

Revision ID: e8b4c12d7a90
Revises: d4a91e72bc10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.database import GUID


revision: str = "e8b4c12d7a90"
down_revision: Union[str, None] = "d4a91e72bc10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=120), nullable=True))
    op.create_index("ix_users_display_name", "users", ["display_name"], unique=False)
    op.create_table(
        "product_tags",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("canonical_product_id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("normalized_name", sa.String(length=50), nullable=False),
        sa.Column("created_by_id", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["canonical_product_id"], ["canonical_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_tags_canonical_product_id", "product_tags", ["canonical_product_id"], unique=False)
    op.create_index("ix_product_tags_normalized_name", "product_tags", ["normalized_name"], unique=False)
    op.create_index(
        "uq_product_tag_product_name", "product_tags",
        ["canonical_product_id", "normalized_name"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_product_tag_product_name", table_name="product_tags")
    op.drop_index("ix_product_tags_normalized_name", table_name="product_tags")
    op.drop_index("ix_product_tags_canonical_product_id", table_name="product_tags")
    op.drop_table("product_tags")
    op.drop_index("ix_users_display_name", table_name="users")
    op.drop_column("users", "display_name")
