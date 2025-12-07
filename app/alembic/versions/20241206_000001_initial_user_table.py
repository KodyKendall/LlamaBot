"""Initial user table with role field

Revision ID: 001
Revises:
Create Date: 2024-12-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Check if user table exists (might exist from SQLModel.metadata.create_all)
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'user' not in inspector.get_table_names():
        # Create the user table
        op.create_table('user',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('username', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
            sa.Column('password_hash', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('is_admin', sa.Boolean(), nullable=False),
            sa.Column('role', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default='engineer'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_user_username'), 'user', ['username'], unique=True)
    else:
        # Table exists - check if role column exists
        columns = [col['name'] for col in inspector.get_columns('user')]
        if 'role' not in columns:
            op.add_column('user', sa.Column('role', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default='engineer'))


def downgrade() -> None:
    op.drop_index(op.f('ix_user_username'), table_name='user')
    op.drop_table('user')
