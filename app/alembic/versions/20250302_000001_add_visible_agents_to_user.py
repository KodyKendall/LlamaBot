"""Add visible_agents column to user table

Revision ID: 003
Revises: 002
Create Date: 2025-03-02
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('user')]
    if 'visible_agents' not in columns:
        op.add_column('user', sa.Column('visible_agents', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('user', 'visible_agents')
