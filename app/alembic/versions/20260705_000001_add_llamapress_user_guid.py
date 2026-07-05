"""Add llamapress_user_guid / email / display_name to user (Unified Login)

Revision ID: 009
Revises: 008
Create Date: 2026-07-05

Unified Login Phase 2: shadow users keyed by a stable ``llamapress_user_guid``
minted by the mothership. ``init_db()``'s create_all makes these columns on a
FRESH table, but on an existing fleet ``user`` already exists, so this migration
adds the columns + the PARTIAL unique index. Idempotent (inspects columns/indexes
before adding). ``password_hash`` stays NOT NULL — shadow users get an unusable
bcrypt hash, so existing password login is untouched.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = '009'
down_revision: Union[str, None] = '008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('user')]

    if 'llamapress_user_guid' not in columns:
        op.add_column('user', sa.Column('llamapress_user_guid', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    if 'email' not in columns:
        op.add_column('user', sa.Column('email', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True))
    if 'display_name' not in columns:
        op.add_column('user', sa.Column('display_name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True))

    existing_indexes = {ix['name'] for ix in inspector.get_indexes('user')}
    if 'ix_user_llamapress_user_guid' not in existing_indexes:
        # Partial unique index: only non-NULL guids collide, so any number of
        # legacy (guid IS NULL) users coexist. Dialect-specific WHERE clauses —
        # both postgres (prod/dev) and sqlite (tests) support partial indexes.
        op.create_index(
            'ix_user_llamapress_user_guid',
            'user',
            ['llamapress_user_guid'],
            unique=True,
            postgresql_where=sa.text('llamapress_user_guid IS NOT NULL'),
            sqlite_where=sa.text('llamapress_user_guid IS NOT NULL'),
        )


def downgrade() -> None:
    op.drop_index('ix_user_llamapress_user_guid', table_name='user')
    op.drop_column('user', 'display_name')
    op.drop_column('user', 'email')
    op.drop_column('user', 'llamapress_user_guid')
