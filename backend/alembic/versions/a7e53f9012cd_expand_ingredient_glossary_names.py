"""expand ingredient glossary names

Revision ID: a7e53f9012cd
Revises: 8d61c0be7301
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7e53f9012cd"
down_revision: Union[str, None] = "8d61c0be7301"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode emits regular ALTER COLUMN statements on PostgreSQL while
    # using SQLite's required table-copy strategy in the test environment.
    with op.batch_alter_table("ingredient_definitions") as batch_op:
        batch_op.alter_column(
            "name", existing_type=sa.String(length=255), type_=sa.Text(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "normalized_name", existing_type=sa.String(length=255), type_=sa.Text(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "common_name", existing_type=sa.String(length=255), type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("ingredient_definitions") as batch_op:
        batch_op.alter_column(
            "common_name", existing_type=sa.Text(), type_=sa.String(length=255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "normalized_name", existing_type=sa.Text(), type_=sa.String(length=255),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "name", existing_type=sa.Text(), type_=sa.String(length=255),
            existing_nullable=False,
        )
