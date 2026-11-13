"""Separate security and system administrators; add the audit trail (LP-32).

Revision ID: 010
Revises: 009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '010'
down_revision: Union[str, None] = '009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = [col['name'] for col in inspector.get_columns('user')]
    for name in ('is_security_admin', 'is_system_admin'):
        if name not in columns:
            op.add_column('user', sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))

    if 'auditevent' not in inspector.get_table_names():
        op.create_table(
            'auditevent',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('event_type', sa.String(length=40), nullable=False),
            sa.Column('actor', sa.String(length=50), nullable=True),
            sa.Column('subject', sa.String(length=50), nullable=True),
            sa.Column('detail', sa.String(length=1000), nullable=False, server_default=''),
        )
        op.create_index('ix_auditevent_created_at', 'auditevent', ['created_at'])
        op.create_index('ix_auditevent_event_type', 'auditevent', ['event_type'])


def downgrade() -> None:
    op.drop_table('auditevent')
    op.drop_column('user', 'is_system_admin')
    op.drop_column('user', 'is_security_admin')
