"""Count every transcript again: names, vocabulary, silences and exclamations.

Revision ID: e2f7a4b9c613
Revises: d8b4c2e6f150
"""

from alembic import op

revision = "e2f7a4b9c613"
down_revision = "d8b4c2e6f150"
branch_labels = None
depends_on = None


def upgrade():
    # The worker counts transcripts whose key differs, a few hundred a cycle.
    op.execute("UPDATE chapter_states SET talk_stats_key = NULL")


def downgrade():
    pass
