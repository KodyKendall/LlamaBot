"""Add agent_system_prompts table for mothership-delivered system prompts

Revision ID: 008
Revises: 007
Create Date: 2026-06-27

Runtime cache of versioned agent system prompts pulled from the mothership over
the check_updates round-trip. `init_db()`'s create_all already makes this table
on boot; this migration exists for parity with the repo's history and is
idempotent (inspects tables before create_table).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '008'
down_revision: Union[str, None] = '007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: skip if the table already exists (create_all may have made it).
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'agent_system_prompts' not in tables:
        op.create_table(
            'agent_system_prompts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('agent_mode', sa.String(length=100), nullable=False),
            sa.Column('version', sa.String(length=64), nullable=False),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(
            op.f('ix_agent_system_prompts_agent_mode'),
            'agent_system_prompts',
            ['agent_mode'],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_agent_system_prompts_agent_mode'),
        table_name='agent_system_prompts',
    )
    op.drop_table('agent_system_prompts')
