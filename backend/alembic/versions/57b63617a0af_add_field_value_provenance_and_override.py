"""add_field_value_provenance_and_override

Revision ID: 57b63617a0af
Revises: 85a02c69bb35
Create Date: 2026-07-13 17:29:07.105473

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '57b63617a0af'
down_revision: Union[str, None] = '85a02c69bb35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('field_values', sa.Column('override_reason', sa.Text(), nullable=True))
    op.add_column('field_values', sa.Column('evidence', sa.JSON(), nullable=True))
    op.add_column('field_values', sa.Column('reasoning_summary', sa.Text(), nullable=True))
    op.add_column('field_values', sa.Column('semantic_status', sa.String(length=100), nullable=True))
    op.add_column('field_values', sa.Column('semantic_status_type', sa.String(length=100), nullable=True))
    op.add_column('formulation_ingredients', sa.Column('evidence', sa.JSON(), nullable=True))
    op.add_column('formulation_ingredients', sa.Column('key_ingredient_status', sa.String(length=100), nullable=True))

    # AuditLog check constraint update using batch mode
    with op.batch_alter_table('audit_logs') as batch_op:
        try:
            batch_op.drop_constraint('check_audit_action_type', type_='check')
        except Exception:
            pass
        batch_op.create_check_constraint(
            'check_audit_action_type',
            sa.column('action').in_(['create', 'update', 'merge', 'approve', 'reject', 'override'])
        )


def downgrade() -> None:
    # Revert AuditLog check constraint
    with op.batch_alter_table('audit_logs') as batch_op:
        try:
            batch_op.drop_constraint('check_audit_action_type', type_='check')
        except Exception:
            pass
        batch_op.create_check_constraint(
            'check_audit_action_type',
            sa.column('action').in_(['create', 'update', 'merge', 'approve', 'reject'])
        )

    op.drop_column('formulation_ingredients', 'key_ingredient_status')
    op.drop_column('formulation_ingredients', 'evidence')
    op.drop_column('field_values', 'semantic_status_type')
    op.drop_column('field_values', 'semantic_status')
    op.drop_column('field_values', 'reasoning_summary')
    op.drop_column('field_values', 'evidence')
    op.drop_column('field_values', 'override_reason')
