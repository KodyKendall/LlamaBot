"""Change prompt.content column to TEXT type for unlimited length

Revision ID: 005
Revises: 004
Create Date: 2025-03-08
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Change prompt.content from varchar(10000) to TEXT
    # TEXT in PostgreSQL has no practical length limit (up to 1GB)
    # This matches Rails' text column behavior
    op.alter_column(
        'prompt',
        'content',
        type_=sa.Text(),
        existing_type=sa.String(length=10000),
        existing_nullable=False
    )


def downgrade() -> None:
    # Note: downgrade may truncate data if content exceeds 10000 chars
    op.alter_column(
        'prompt',
        'content',
        type_=sa.String(length=10000),
        existing_type=sa.Text(),
        existing_nullable=False
    )
