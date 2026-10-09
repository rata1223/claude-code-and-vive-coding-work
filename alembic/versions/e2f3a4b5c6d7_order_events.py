"""order_events

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-10-09 08:00:00.000000

Append-only history of ``orders`` (P2-01): one row per insert and per change of
status, fill or broker order number, written by a session hook in the same
transaction as the change (``backend/database/order_history.py``).

On Postgres a trigger also refuses UPDATE and DELETE on the table, so the log
stays append-only below the ORM too. Databases built by ``create_all`` get the
table but not the trigger.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('order_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('from_status', sa.String(length=20), nullable=True),
    sa.Column('to_status', sa.String(length=20), nullable=False),
    sa.Column('filled_qty', sa.Integer(), nullable=True),
    sa.Column('avg_fill_price', sa.Float(), nullable=True),
    sa.Column('broker_order_id', sa.String(length=50), nullable=True),
    sa.Column('recorded_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_order_events_order_id'), 'order_events', ['order_id'], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION order_events_append_only() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION USING MESSAGE = 'order_events is append-only: ' || TG_OP;
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER order_events_append_only
            BEFORE UPDATE OR DELETE ON order_events
            FOR EACH ROW EXECUTE FUNCTION order_events_append_only()
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS order_events_append_only ON order_events")
        op.execute("DROP FUNCTION IF EXISTS order_events_append_only()")
    op.drop_index(op.f('ix_order_events_order_id'), table_name='order_events')
    op.drop_table('order_events')
