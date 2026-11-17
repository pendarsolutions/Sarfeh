"""teams as owners, budgets, and per-resource estimates

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

from alembic import op
import finops_api.db
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table('teams',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('organization_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'name', name='uq_team_name')
    )
    op.create_index(op.f('ix_teams_organization_id'), 'teams', ['organization_id'], unique=False)
    op.create_table('budgets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('organization_id', sa.String(length=36), nullable=False),
    sa.Column('scope', sa.String(length=36), nullable=False),
    sa.Column('monthly_toman', finops_api.db.Money(), nullable=False),
    sa.Column('alerted', JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'scope', name='uq_budget_scope')
    )
    op.create_index(op.f('ix_budgets_organization_id'), 'budgets', ['organization_id'], unique=False)
    for table in ('inventory_resources', 'servers', 'spend_entries'):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('team_id', sa.String(length=36), nullable=True))
    with op.batch_alter_table('cost_snapshots') as batch:
        batch.add_column(sa.Column('resource_totals', JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cost_snapshots') as batch:
        batch.drop_column('resource_totals')
    for table in ('spend_entries', 'servers', 'inventory_resources'):
        with op.batch_alter_table(table) as batch:
            batch.drop_column('team_id')
    op.drop_index(op.f('ix_budgets_organization_id'), table_name='budgets')
    op.drop_table('budgets')
    op.drop_index(op.f('ix_teams_organization_id'), table_name='teams')
    op.drop_table('teams')
