"""Add skills table for stackable prompt skills

Revision ID: 006
Revises: 005
Create Date: 2025-03-09
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Check if table already exists (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'skill' not in tables:
        op.create_table(
            'skill',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('description', sa.String(length=500), nullable=True),
            sa.Column('group', sa.String(length=50), nullable=False, server_default='General'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
            sa.Column('usage_count', sa.Integer(), nullable=False, server_default='0'),
            sa.PrimaryKeyConstraint('id')
        )
        # Create indexes
        op.create_index(op.f('ix_skill_name'), 'skill', ['name'], unique=False)
        op.create_index(op.f('ix_skill_group'), 'skill', ['group'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_skill_group'), table_name='skill')
    op.drop_index(op.f('ix_skill_name'), table_name='skill')
    op.drop_table('skill')
