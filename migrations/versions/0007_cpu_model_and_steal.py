"""the CPU model of a monitored server, and the CPU time its host takes away

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('servers', sa.Column('cpu_model', sa.String(length=200), nullable=True))
    op.add_column('servers', sa.Column('cpu_mhz', sa.Integer(), nullable=True))
    op.add_column('servers', sa.Column('collector_version', sa.Integer(), nullable=True))
    op.add_column('server_samples', sa.Column('cpu_steal', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('server_samples', 'cpu_steal')
    op.drop_column('servers', 'collector_version')
    op.drop_column('servers', 'cpu_mhz')
    op.drop_column('servers', 'cpu_model')
