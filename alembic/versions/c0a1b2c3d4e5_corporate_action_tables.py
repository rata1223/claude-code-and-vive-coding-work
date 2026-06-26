"""corporate_action_tables

Revision ID: c0a1b2c3d4e5
Revises: 1aad3f2df8d9
Create Date: 2026-06-24 13:40:00.000000

P2-02C — runtime persistence for corporate actions and adjustment history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0a1b2c3d4e5'
down_revision: Union[str, None] = '1aad3f2df8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('corporate_actions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('broker', sa.String(length=10), nullable=False),
    sa.Column('symbol', sa.String(length=20), nullable=False),
    sa.Column('action_type', sa.String(length=20), nullable=False),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('ratio', sa.Float(), nullable=True),
    sa.Column('cash_amount', sa.Float(), nullable=True),
    sa.Column('new_symbol', sa.String(length=20), nullable=True),
    sa.Column('source', sa.String(length=40), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('detected_at', sa.DateTime(), nullable=False),
    sa.Column('applied_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('broker', 'symbol', 'effective_date', 'action_type', name='uq_corporate_action')
    )
    op.create_index(op.f('ix_corporate_actions_broker'), 'corporate_actions', ['broker'], unique=False)
    op.create_index(op.f('ix_corporate_actions_status'), 'corporate_actions', ['status'], unique=False)
    op.create_index(op.f('ix_corporate_actions_symbol'), 'corporate_actions', ['symbol'], unique=False)
    op.create_table('corporate_action_history',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('corporate_action_id', sa.Integer(), nullable=True),
    sa.Column('broker', sa.String(length=10), nullable=False),
    sa.Column('symbol', sa.String(length=20), nullable=False),
    sa.Column('action_type', sa.String(length=20), nullable=False),
    sa.Column('qty_before', sa.Float(), nullable=True),
    sa.Column('avg_before', sa.Float(), nullable=True),
    sa.Column('qty_after', sa.Float(), nullable=True),
    sa.Column('avg_after', sa.Float(), nullable=True),
    sa.Column('cash_delta', sa.Float(), nullable=False),
    sa.Column('value_preserved', sa.Boolean(), nullable=False),
    sa.Column('applied_at', sa.DateTime(), nullable=False),
    sa.Column('actor', sa.String(length=50), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_corporate_action_history_applied_at'), 'corporate_action_history', ['applied_at'], unique=False)
    op.create_index(op.f('ix_corporate_action_history_corporate_action_id'), 'corporate_action_history', ['corporate_action_id'], unique=False)
    op.create_index(op.f('ix_corporate_action_history_symbol'), 'corporate_action_history', ['symbol'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_corporate_action_history_symbol'), table_name='corporate_action_history')
    op.drop_index(op.f('ix_corporate_action_history_corporate_action_id'), table_name='corporate_action_history')
    op.drop_index(op.f('ix_corporate_action_history_applied_at'), table_name='corporate_action_history')
    op.drop_table('corporate_action_history')
    op.drop_index(op.f('ix_corporate_actions_symbol'), table_name='corporate_actions')
    op.drop_index(op.f('ix_corporate_actions_status'), table_name='corporate_actions')
    op.drop_index(op.f('ix_corporate_actions_broker'), table_name='corporate_actions')
    op.drop_table('corporate_actions')
