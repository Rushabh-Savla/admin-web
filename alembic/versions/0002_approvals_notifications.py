"""0002_approval_workflow_and_notifications

Revision ID: 0002_approval_workflow_and_notifications
Revises: 0001_initial_schema
Create Date: 2026-10-16 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '0002_approval_workflow_and_notifications'
down_revision: Union[str, None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. assignment_approvals
    op.create_table(
        'assignment_approvals',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('assignment_id', sa.BigInteger(), nullable=False),
        sa.Column('dataset_id', sa.BigInteger(), nullable=False),
        sa.Column('annotator_id', sa.BigInteger(), nullable=False),
        sa.Column('reviewer_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('denial_reason', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['annotator_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['reviewer_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_assignment_approvals_status', 'assignment_approvals', ['status'])
    op.create_index('idx_assignment_approvals_assignment', 'assignment_approvals', ['assignment_id'])
    op.create_index('idx_assignment_approvals_dataset', 'assignment_approvals', ['dataset_id'])

    # 2. notifications
    op.create_table(
        'notifications',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('type', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=True),
        sa.Column('assignment_id', sa.BigInteger(), nullable=True),
        sa.Column('action_url', sa.Text(), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_notifications_user_read', 'notifications', ['user_id', 'is_read'])
    op.create_index('idx_notifications_created', 'notifications', ['created_at'])


def downgrade() -> None:
    op.drop_index('idx_notifications_created', table_name='notifications')
    op.drop_index('idx_notifications_user_read', table_name='notifications')
    op.drop_table('notifications')

    op.drop_index('idx_assignment_approvals_dataset', table_name='assignment_approvals')
    op.drop_index('idx_assignment_approvals_assignment', table_name='assignment_approvals')
    op.drop_index('idx_assignment_approvals_status', table_name='assignment_approvals')
    op.drop_table('assignment_approvals')
