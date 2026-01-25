"""Add thread_metadata table for lazy loading optimization

Revision ID: 002
Revises: 001
Create Date: 2025-01-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Check if table already exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'threadmetadata' not in inspector.get_table_names():
        op.create_table('threadmetadata',
            sa.Column('thread_id', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
            sa.Column('title', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False, server_default='New Conversation'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('agent_name', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=True),
            sa.PrimaryKeyConstraint('thread_id'),
            sa.ForeignKeyConstraint(['user_id'], ['user.id'], )
        )
        op.create_index(op.f('ix_threadmetadata_user_id'), 'threadmetadata', ['user_id'])
        op.create_index(op.f('ix_threadmetadata_updated_at'), 'threadmetadata', ['updated_at'])


def downgrade() -> None:
    op.drop_index(op.f('ix_threadmetadata_updated_at'), table_name='threadmetadata')
    op.drop_index(op.f('ix_threadmetadata_user_id'), table_name='threadmetadata')
    op.drop_table('threadmetadata')
