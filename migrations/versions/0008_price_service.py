"""this installation and Sarfeh's price service: its token, the newest catalog, the applied one

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table('price_service',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(length=200), nullable=True),
    sa.Column('registered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_url', sa.String(length=300), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('check_error', sa.Text(), nullable=True),
    sa.Column('latest', JSONType, nullable=True),
    sa.Column('applied', JSONType, nullable=True),
    sa.Column('notified_digest', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('price_service')
