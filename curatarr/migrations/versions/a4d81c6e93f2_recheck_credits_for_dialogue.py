"""Check credits markers again for dialogue inside them.

A new rule trims (or hides) credits markers that people are still talking in,
which earlier checks didn't look for. Only the marker step is redone: recaps,
previews and markers already hidden are kept.

Revision ID: a4d81c6e93f2
Revises: f3c9d2a7b815
"""

from alembic import op

revision = "a4d81c6e93f2"
down_revision = "f3c9d2a7b815"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE chapter_states SET markers_checked_at = NULL
        WHERE markers_checked_at IS NOT NULL
          AND media_part_id IN (SELECT id FROM media_parts WHERE kind = 'episode')
        """
    )


def downgrade():
    pass
