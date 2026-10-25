"""Try the seek index repair again for files it gave up on: mkvmerge put
their video track first, so the check found the copy different from the
original. It now keeps the file's own track order.

Revision ID: d81f3b6a2c94
Revises: c4e1a9d36f02
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "d81f3b6a2c94"
down_revision = "c4e1a9d36f02"
branch_labels = None
depends_on = None

GAVE_UP = "The rebuilt copy didn't match the original"


def upgrade():
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, seek_layout FROM chapter_states WHERE seek_layout IS NOT NULL"
        )
    ).fetchall()
    again = []
    for row_id, layout in rows:
        if isinstance(layout, str):
            try:
                layout = json.loads(layout)
            except ValueError:
                continue
        if isinstance(layout, dict) and layout.get("error") == GAVE_UP:
            again.append(row_id)
    for row_id in again:
        bind.execute(
            sa.text("UPDATE chapter_states SET seek_layout_size = NULL WHERE id = :id"),
            {"id": row_id},
        )


def downgrade():
    pass  # a file looked at again is no different
