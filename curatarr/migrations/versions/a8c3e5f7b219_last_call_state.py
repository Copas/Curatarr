"""Last call: a candidate state before Leaving Soon (0.2.0rc119).

The partial unique index allowing one active candidate per title lists the
active states, so it's made again with LAST_CALL among them.

Revision ID: a8c3e5f7b219
Revises: f4b8d2e6a913
"""

import sqlalchemy as sa
from alembic import op

revision = "a8c3e5f7b219"
down_revision = "f4b8d2e6a913"
branch_labels = None
depends_on = None

BEFORE = ("ELIGIBLE", "REVIEW", "LEAVING_SOON", "SNOOZED", "APPROVED", "EXECUTING")
AFTER = (
    "ELIGIBLE",
    "REVIEW",
    "LAST_CALL",
    "LEAVING_SOON",
    "SNOOZED",
    "APPROVED",
    "EXECUTING",
)


def _index(states):
    where = sa.text("state IN (" + ", ".join(f"'{state}'" for state in states) + ")")
    op.create_index(
        "uq_active_candidate",
        "purge_candidates",
        ["media_identity_id"],
        unique=True,
        sqlite_where=where,
        postgresql_where=where,
    )


def upgrade():
    op.drop_index("uq_active_candidate", table_name="purge_candidates")
    _index(AFTER)


def downgrade():
    op.execute(
        "UPDATE purge_candidates SET state = 'RESCUED' WHERE state = 'LAST_CALL'"
    )
    op.drop_index("uq_active_candidate", table_name="purge_candidates")
    _index(BEFORE)
