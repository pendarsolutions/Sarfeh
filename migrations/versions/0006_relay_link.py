"""registration with the shared Bale bot's relay

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('relay_link',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('relay_url', sa.String(length=300), nullable=False),
    sa.Column('install_id', sa.String(length=64), nullable=False),
    sa.Column('secret', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('relay_link')
