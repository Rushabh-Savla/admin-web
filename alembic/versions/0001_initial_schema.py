"""0001_initial_schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-14 16:55:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. users
    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False, server_default='ANNOTATOR'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username')
    )
    op.create_index('ix_users_username', 'users', ['username'])

    # 2. devices
    op.create_table(
        'devices',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('device_identifier', sa.String(length=128), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('token_hash', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_identifier')
    )
    op.create_index('ix_devices_identifier', 'devices', ['device_identifier'])

    # 3. datasets
    op.create_table(
        'datasets',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('assigned_under', sa.String(length=128), nullable=True),
        sa.Column('project_status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
        sa.Column('remote_name', sa.String(length=64), nullable=False),
        sa.Column('remote_path', sa.Text(), nullable=False),
        sa.Column('catalog_state', sa.String(length=32), nullable=False, server_default='IDLE'),
        sa.Column('current_catalog_version', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )

    # 4. dataset_directories
    op.create_table(
        'dataset_directories',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('dataset_id', sa.BigInteger(), nullable=False),
        sa.Column('normalized_path', sa.Text(), nullable=False),
        sa.Column('parent_path', sa.Text(), nullable=True),
        sa.Column('direct_image_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('direct_json_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('direct_txt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('direct_other_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('recursive_image_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('recursive_json_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('recursive_txt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('recursive_other_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dataset_id', 'normalized_path', name='uq_dataset_dir_path')
    )
    op.create_index('idx_dataset_dir_parent', 'dataset_directories', ['dataset_id', 'parent_path'])

    # 5. dataset_files
    op.create_table(
        'dataset_files',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('dataset_id', sa.BigInteger(), nullable=False),
        sa.Column('normalized_path', sa.Text(), nullable=False),
        sa.Column('basename', sa.String(length=255), nullable=False),
        sa.Column('extension', sa.String(length=32), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('remote_mtime', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
        sa.Column('catalog_version', sa.String(length=64), nullable=False),
        sa.Column('file_state', sa.String(length=32), nullable=False, server_default='ELIGIBLE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dataset_id', 'normalized_path', name='uq_dataset_file_path')
    )
    op.create_index('idx_dataset_file_lookup', 'dataset_files', ['dataset_id', 'category', 'file_state'])
    op.create_index('idx_dataset_file_cat_ver', 'dataset_files', ['dataset_id', 'catalog_version'])

    # 6. assignments
    op.create_table(
        'assignments',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('dataset_id', sa.BigInteger(), nullable=False),
        sa.Column('directory_scope', sa.Text(), nullable=False),
        sa.Column('scope_type', sa.String(length=16), nullable=False, server_default='RECURSIVE'),
        sa.Column('annotator_id', sa.BigInteger(), nullable=False),
        sa.Column('creator_id', sa.BigInteger(), nullable=False),
        sa.Column('requested_count', sa.Integer(), nullable=False),
        sa.Column('allocated_count', sa.Integer(), nullable=False),
        sa.Column('batch_size', sa.Integer(), nullable=True, server_default='100'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PREPARING'),
        sa.Column('catalog_version', sa.String(length=64), nullable=False),
        sa.Column('workspace_path', sa.Text(), nullable=True),
        sa.Column('lease_generation', sa.BigInteger(), nullable=False, server_default='1'),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['annotator_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['creator_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )

    # 7. assignment_items
    op.create_table(
        'assignment_items',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('assignment_id', sa.BigInteger(), nullable=False),
        sa.Column('file_id', sa.BigInteger(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('item_state', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_id'], ['dataset_files.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('assignment_id', 'file_id', name='uq_assignment_item_file'),
        sa.UniqueConstraint('assignment_id', 'ordinal', name='uq_assignment_item_ordinal')
    )
    op.create_index('idx_assignment_items_state', 'assignment_items', ['assignment_id', 'item_state'])

    # 8. active_image_claims (CRITICAL SINGLE-OWNER CONSTRAINT)
    op.create_table(
        'active_image_claims',
        sa.Column('file_id', sa.BigInteger(), nullable=False),
        sa.Column('assignment_id', sa.BigInteger(), nullable=False),
        sa.Column('annotator_id', sa.BigInteger(), nullable=False),
        sa.Column('claim_generation', sa.BigInteger(), nullable=False, server_default='1'),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_id'], ['dataset_files.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['annotator_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('file_id')
    )

    # 9. annotation_submissions
    op.create_table(
        'annotation_submissions',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('assignment_id', sa.BigInteger(), nullable=False),
        sa.Column('file_id', sa.BigInteger(), nullable=False),
        sa.Column('annotator_id', sa.BigInteger(), nullable=False),
        sa.Column('ownership_generation', sa.BigInteger(), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('raw_content', sa.Text(), nullable=True),
        sa.Column('validation_state', sa.String(length=32), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['annotator_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignments.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['file_id'], ['dataset_files.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )

    # 10. background_declarations
    op.create_table(
        'background_declarations',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('assignment_id', sa.BigInteger(), nullable=False),
        sa.Column('file_id', sa.BigInteger(), nullable=False),
        sa.Column('annotator_id', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('approval_state', sa.String(length=32), nullable=False, server_default='PENDING_APPROVAL'),
        sa.Column('approved_by', sa.BigInteger(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['annotator_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignments.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['file_id'], ['dataset_files.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id')
    )

    # 11. sync_jobs
    op.create_table(
        'sync_jobs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('job_type', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='QUEUED'),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_retries', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('claimed_worker', sa.String(length=64), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )
    op.create_index('idx_sync_jobs_queue', 'sync_jobs', ['status', 'next_attempt_at'])

    # 12. audit_events
    op.create_table(
        'audit_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('resource_type', sa.String(length=64), nullable=False),
        sa.Column('resource_id', sa.String(length=64), nullable=False),
        sa.Column('details', sa.JSON(), nullable=False),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('audit_events')
    op.drop_table('sync_jobs')
    op.drop_table('background_declarations')
    op.drop_table('annotation_submissions')
    op.drop_table('active_image_claims')
    op.drop_table('assignment_items')
    op.drop_table('assignments')
    op.drop_table('dataset_files')
    op.drop_table('dataset_directories')
    op.drop_table('datasets')
    op.drop_table('devices')
    op.drop_table('users')

