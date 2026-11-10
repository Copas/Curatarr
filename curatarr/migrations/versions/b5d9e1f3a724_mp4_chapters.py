"""MP4 files take chapters now (0.2.0rc133).

Files set aside as an unsupported container that are MP4s go back to
"not processed", so the backlog gives them chapters like any new file.

Revision ID: b5d9e1f3a724
Revises: a8c3e5f7b219
"""

from alembic import op

revision = "b5d9e1f3a724"
down_revision = "a8c3e5f7b219"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE chapter_states SET chapter_status = 'not_processed', "
        "chapter_error = NULL, chapter_processed_at = NULL, attempts = 0 "
        "WHERE chapter_status = 'unsupported_container' "
        "AND (container LIKE '%mp4%' OR container LIKE '%mov%')"
    )


def downgrade():
    pass  # a scan marks any left unprocessed again
