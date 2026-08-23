"""extend audit actions for tags and display names

Revision ID: f19c4b2d7a61
Revises: e8b4c12d7a90
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f19c4b2d7a61"
down_revision: Union[str, None] = "e8b4c12d7a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BASE_ACTIONS = [
    "create", "update", "merge", "approve", "reject", "override",
    "invitation_created", "invitation_resent", "invitation_revoked",
    "invitation_accepted", "user_role_changed", "user_disabled", "user_enabled",
]


def _replace_action_constraint(actions: list[str]) -> None:
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_constraint("check_audit_action_type", type_="check")
        batch_op.create_check_constraint(
            "check_audit_action_type",
            sa.column("action").in_(actions),
        )


def upgrade() -> None:
    _replace_action_constraint(BASE_ACTIONS + ["user_name_changed", "tags_updated"])


def downgrade() -> None:
    _replace_action_constraint(BASE_ACTIONS)
