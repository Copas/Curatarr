"""Count every transcript again: catchphrases now skip theme songs and credits.

Revision ID: d8b4c2e6f150
Revises: c5a1f8e3d927
"""

from alembic import op

revision = "d8b4c2e6f150"
down_revision = "c5a1f8e3d927"
branch_labels = None
depends_on = None


def upgrade():
    # The worker counts transcripts whose key differs, a few hundred a cycle.
    op.execute("UPDATE chapter_states SET talk_stats_key = NULL")


def downgrade():
    pass
