"""add external viewer role

Revision ID: 24a9b7e3c601
Revises: f19c4b2d7a61
"""
from typing import Sequence, Union

from alembic import op


revision: str = "24a9b7e3c601"
down_revision: Union[str, None] = "f19c4b2d7a61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _roles(values: tuple[str, ...]) -> None:
    expression = "role IN (" + ", ".join(f"'{value}'" for value in values) + ")"
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("check_user_role", type_="check")
        batch_op.create_check_constraint("check_user_role", expression)
    with op.batch_alter_table("user_invitations") as batch_op:
        batch_op.drop_constraint("check_invitation_role", type_="check")
        batch_op.create_check_constraint("check_invitation_role", expression)


def upgrade() -> None:
    _roles(("admin", "editor", "viewer", "external_viewer"))


def downgrade() -> None:
    connection = op.get_bind()
    external_users = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM users WHERE role = 'external_viewer'"
    ).scalar()
    external_invites = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM user_invitations WHERE role = 'external_viewer'"
    ).scalar()
    if external_users or external_invites:
        raise RuntimeError("Cannot remove external_viewer while users or invitations still use it.")
    _roles(("admin", "editor", "viewer"))
