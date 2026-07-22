"""add ingredient knowledge provenance

Revision ID: 8d61c0be7301
Revises: 4c2a91d6f8be
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8d61c0be7301"
down_revision: Union[str, None] = "4c2a91d6f8be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ingredient_definitions", sa.Column("source_name", sa.String(255)))
    op.add_column("ingredient_definitions", sa.Column("source_url", sa.Text()))
    op.add_column("ingredient_definitions", sa.Column("source_record_id", sa.String(255)))
    op.add_column("ingredient_definitions", sa.Column("regulatory_status", sa.String(100)))
    op.add_column("ingredient_definitions", sa.Column("cas_number", sa.String(100)))
    op.add_column("ingredient_definitions", sa.Column("ec_number", sa.String(100)))
    op.create_index(
        "ix_ingredient_definitions_source_record_id",
        "ingredient_definitions",
        ["source_record_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingredient_definitions_source_record_id", table_name="ingredient_definitions")
    for column in (
        "ec_number", "cas_number", "regulatory_status", "source_record_id",
        "source_url", "source_name",
    ):
        op.drop_column("ingredient_definitions", column)
