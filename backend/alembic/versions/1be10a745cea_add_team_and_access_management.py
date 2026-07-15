"""add_team_and_access_management

Revision ID: 1be10a745cea
Revises: 57b63617a0af
Create Date: 2026-07-15 02:11:18.597857

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.database


# revision identifiers, used by Alembic.
revision: str = '1be10a745cea'
down_revision: Union[str, None] = '57b63617a0af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Normalize existing database user emails and check for collisions
    connection = op.get_bind()
    users = connection.execute(sa.text("SELECT id, email FROM users")).fetchall()
    
    seen_emails = {}
    for user_id, email in users:
        norm_email = email.strip().lower()
        if norm_email in seen_emails:
            orig_id = seen_emails[norm_email]
            raise ValueError(f"Email collision detected: '{email}' (ID: {user_id}) conflicts with ID: {orig_id} under case-insensitive normalization.")
        seen_emails[norm_email] = user_id

    # Update existing emails to lowercase
    for user_id, email in users:
        norm_email = email.strip().lower()
        connection.execute(
            sa.text("UPDATE users SET email = :email WHERE id = :id"),
            {"email": norm_email, "id": user_id}
        )

    # 2. Schema changes
    op.create_table('user_invitations',
    sa.Column('id', app.database.GUID(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=50), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('invited_by_id', app.database.GUID(), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sent_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('resend_count', sa.Integer(), nullable=False),
    sa.Column('email_delivery_status', sa.String(length=50), nullable=True),
    sa.Column('email_delivery_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("role IN ('admin', 'editor', 'viewer')", name='check_invitation_role'),
    sa.CheckConstraint("status IN ('pending', 'accepted', 'revoked', 'expired')", name='check_invitation_status'),
    sa.ForeignKeyConstraint(['invited_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_invitations_email'), 'user_invitations', ['email'], unique=False)
    op.create_index(op.f('ix_user_invitations_token_hash'), 'user_invitations', ['token_hash'], unique=True)
    op.create_index('uq_invitation_pending_email', 'user_invitations', ['email'], unique=True, sqlite_where=sa.text("status = 'pending'"), postgresql_where=sa.text("status = 'pending'"))
    
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), server_default=sa.text('1'), nullable=False))
        batch_op.add_column(sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('invited_by_id', app.database.GUID(), nullable=True))
        batch_op.add_column(sa.Column('accepted_invitation_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key('fk_users_invited_by_id', 'users', ['invited_by_id'], ['id'], ondelete='SET NULL')

    # Update AuditLog constraints
    with op.batch_alter_table('audit_logs') as batch_op:
        try:
            batch_op.drop_constraint('check_audit_actor_type', type_='check')
        except Exception:
            pass
        try:
            batch_op.drop_constraint('check_audit_action_type', type_='check')
        except Exception:
            pass
        batch_op.create_check_constraint(
            'check_audit_actor_type',
            sa.column('actor_type').in_(['user', 'system', 'ai', 'rule', 'invited_user'])
        )
        batch_op.create_check_constraint(
            'check_audit_action_type',
            sa.column('action').in_([
                'create', 'update', 'merge', 'approve', 'reject', 'override',
                'invitation_created', 'invitation_resent', 'invitation_revoked', 'invitation_accepted',
                'user_role_changed', 'user_disabled', 'user_enabled'
            ])
        )


def downgrade() -> None:
    # Revert AuditLog constraints
    with op.batch_alter_table('audit_logs') as batch_op:
        try:
            batch_op.drop_constraint('check_audit_actor_type', type_='check')
        except Exception:
            pass
        try:
            batch_op.drop_constraint('check_audit_action_type', type_='check')
        except Exception:
            pass
        batch_op.create_check_constraint(
            'check_audit_actor_type',
            sa.column('actor_type').in_(['user', 'system', 'ai', 'rule'])
        )
        batch_op.create_check_constraint(
            'check_audit_action_type',
            sa.column('action').in_(['create', 'update', 'merge', 'approve', 'reject', 'override'])
        )

    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_constraint('fk_users_invited_by_id', type_='foreignkey')
        batch_op.drop_column('disabled_at')
        batch_op.drop_column('accepted_invitation_at')
        batch_op.drop_column('invited_by_id')
        batch_op.drop_column('last_login_at')
        batch_op.drop_column('is_active')

    op.drop_index('uq_invitation_pending_email', table_name='user_invitations', sqlite_where=sa.text("status = 'pending'"), postgresql_where=sa.text("status = 'pending'"))
    op.drop_index(op.f('ix_user_invitations_token_hash'), table_name='user_invitations')
    op.drop_index(op.f('ix_user_invitations_email'), table_name='user_invitations')
    op.drop_table('user_invitations')
