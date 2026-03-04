"""Add SchedulerInvocationLog table and error fields to ScheduledJobRun

Revision ID: 004
Revises: 003
Create Date: 2025-03-03
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Create SchedulerInvocationLog table if it doesn't exist
    existing_tables = inspector.get_table_names()
    if 'schedulerinvocationlog' not in existing_tables:
        op.create_table(
            'schedulerinvocationlog',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('invoked_at', sa.DateTime(), nullable=False, index=True),
            sa.Column('source_ip', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=True),
            sa.Column('auth_method', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default='scheduler_token'),
            sa.Column('auth_user_id', sa.Integer(), nullable=True),
            sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default='success'),
            sa.Column('jobs_checked', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('jobs_executed', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('error_type', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
            sa.Column('error_message', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=True),
            sa.Column('duration_ms', sa.Integer(), nullable=True),
        )

    # Add error_type and error_traceback to ScheduledJobRun if they don't exist
    if 'scheduledjobrun' in existing_tables:
        columns = [col['name'] for col in inspector.get_columns('scheduledjobrun')]
        if 'error_type' not in columns:
            op.add_column('scheduledjobrun', sa.Column('error_type', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True))
        if 'error_traceback' not in columns:
            op.add_column('scheduledjobrun', sa.Column('error_traceback', sqlmodel.sql.sqltypes.AutoString(length=5000), nullable=True))


def downgrade() -> None:
    op.drop_table('schedulerinvocationlog')
    op.drop_column('scheduledjobrun', 'error_type')
    op.drop_column('scheduledjobrun', 'error_traceback')
