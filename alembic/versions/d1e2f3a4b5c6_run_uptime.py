"""run_uptime

Revision ID: d1e2f3a4b5c6
Revises: c0a1b2c3d4e5
Create Date: 2026-10-04 08:00:00.000000

The 4-week paper gate counts the time a run was actually running, not calendar
days: one row per unbroken stretch of a run's session (start, last beat, clean
end).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'c0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('run_uptime',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('boot_at', sa.DateTime(), nullable=False),
    sa.Column('last_beat_at', sa.DateTime(), nullable=False),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_run_uptime_run_id'), 'run_uptime', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_run_uptime_run_id'), table_name='run_uptime')
    op.drop_table('run_uptime')
