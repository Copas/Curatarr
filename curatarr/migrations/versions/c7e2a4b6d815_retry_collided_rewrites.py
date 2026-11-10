"""Retry file changes that failed when two ran at once (0.2.0rc134).

TV-friendly audio, subtitle tracks, default audio and seek repair sometimes
rewrote one file at the same moment; both copies failed their checks and
the file was left as it was, recorded as failed, never tried again. Each
change now waits for the file's lock, so those failures are cleared to be
tried again.

Revision ID: c7e2a4b6d815
Revises: b5d9e1f3a724
"""

from alembic import op

revision = "c7e2a4b6d815"
down_revision = "b5d9e1f3a724"
branch_labels = None
depends_on = None

COLUMNS = ("compat_audio", "subtitle_tracks", "default_audio", "seek_layout")


def upgrade():
    for column in COLUMNS:
        op.execute(
            f"UPDATE chapter_states SET {column} = NULL, {column}_size = NULL "
            f"WHERE {column} LIKE '%\"error\"%'"
        )


def downgrade():
    pass
