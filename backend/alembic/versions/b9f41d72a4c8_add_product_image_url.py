"""add product image URL

Revision ID: b9f41d72a4c8
Revises: a7e53f9012cd
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9f41d72a4c8"
down_revision: Union[str, None] = "a7e53f9012cd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("canonical_products", sa.Column("image_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("canonical_products", "image_url")
