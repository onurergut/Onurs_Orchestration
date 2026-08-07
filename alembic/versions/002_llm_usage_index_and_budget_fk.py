"""llm usage project month index and budget state fk ondelete

Revision ID: 94406dba1960
Revises: 9592bd6ffa10
Create Date: 2026-08-07 10:31:04.398409

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '94406dba1960'
down_revision: Union[str, None] = '9592bd6ffa10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # date_trunc('month', timestamptz) is STABLE (depends on session timezone), so Postgres
    # rejects it directly as an index expression. Wrap in an IMMUTABLE function pinned to UTC.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION immutable_date_trunc_month(ts timestamptz)
        RETURNS timestamptz AS $$
            SELECT date_trunc('month', $1 AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
        $$ LANGUAGE SQL IMMUTABLE
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_project_month "
        "ON llm_usage (project_id, immutable_date_trunc_month(ts))"
    )
    op.execute("ALTER TABLE budget_state DROP CONSTRAINT IF EXISTS budget_state_last_run_id_fkey")
    op.execute(
        "ALTER TABLE budget_state ADD CONSTRAINT budget_state_last_run_id_fkey "
        "FOREIGN KEY (last_run_id) REFERENCES runs(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_llm_usage_project_month")
    op.execute("ALTER TABLE budget_state DROP CONSTRAINT IF EXISTS budget_state_last_run_id_fkey")
    op.execute(
        "ALTER TABLE budget_state ADD CONSTRAINT budget_state_last_run_id_fkey "
        "FOREIGN KEY (last_run_id) REFERENCES runs(id)"
    )
    op.execute("DROP FUNCTION IF EXISTS immutable_date_trunc_month(timestamptz)")