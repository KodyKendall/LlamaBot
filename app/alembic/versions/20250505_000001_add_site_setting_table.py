"""Add site_setting table for instance-wide settings

Revision ID: 007
Revises: 006
Create Date: 2025-05-05
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'sitesetting' not in inspector.get_table_names():
        op.create_table(
            'sitesetting',
            sa.Column('key', sa.String(length=100), primary_key=True),
            sa.Column('value', sa.String(length=1000), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table('sitesetting')
