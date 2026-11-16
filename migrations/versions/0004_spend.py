"""spend ledger, monthly cost snapshots, and the last confirmed version of an inventory

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

from alembic import op
import finops_api.db
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table('spend_entries',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('organization_id', sa.String(length=36), nullable=False),
    sa.Column('month', sa.String(length=7), nullable=False),
    sa.Column('provider_id', sa.String(length=50), nullable=True),
    sa.Column('vendor', sa.String(length=200), nullable=True),
    sa.Column('amount_toman', finops_api.db.Money(), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('paid_on', sa.Date(), nullable=True),
    sa.Column('reference', sa.String(length=200), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('upload_id', sa.String(length=36), nullable=True),
    sa.Column('inventory_id', sa.String(length=36), nullable=True),
    sa.Column('evidence', JSON, nullable=False),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_spend_entries_org_month', 'spend_entries', ['organization_id', 'month'], unique=False)
    op.create_table('cost_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('organization_id', sa.String(length=36), nullable=False),
    sa.Column('inventory_id', sa.String(length=36), nullable=False),
    sa.Column('month', sa.String(length=7), nullable=False),
    sa.Column('quote_id', sa.String(length=36), nullable=False),
    sa.Column('complete', sa.Boolean(), nullable=False),
    sa.Column('total_toman', finops_api.db.Money(), nullable=True),
    sa.Column('priced_subtotal_toman', finops_api.db.Money(), nullable=False),
    sa.Column('provider_totals', JSON, nullable=False),
    sa.Column('trigger', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['inventory_id'], ['inventories.id'], ),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['quote_id'], ['price_quotes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('inventory_id', 'month', name='uq_cost_snapshot_month')
    )
    op.create_index(op.f('ix_cost_snapshots_organization_id'), 'cost_snapshots', ['organization_id'], unique=False)
    with op.batch_alter_table('inventories') as batch:
        batch.add_column(sa.Column('confirmed_resources', JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('inventories') as batch:
        batch.drop_column('confirmed_resources')
    op.drop_index(op.f('ix_cost_snapshots_organization_id'), table_name='cost_snapshots')
    op.drop_table('cost_snapshots')
    op.drop_index('ix_spend_entries_org_month', table_name='spend_entries')
    op.drop_table('spend_entries')
