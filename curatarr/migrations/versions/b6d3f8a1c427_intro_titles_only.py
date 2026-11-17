"""Intro rule 7: an intro Curatarr adds must look like a title sequence.

Rule 6 added Curatarr's own intro wherever the season shared a stretch of
sound, and some were a music cue playing under the story. Only episodes with
an intro Curatarr added can change under rule 7, so the rest are moved
straight to it instead of each being checked again.

Revision ID: b6d3f8a1c427
Revises: a8c4e2f7b519
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "b6d3f8a1c427"
down_revision = "a8c4e2f7b519"
branch_labels = None
depends_on = None


def _added_intro(hidden) -> bool:
    if isinstance(hidden, str):
        hidden = json.loads(hidden)
    return any(h.get("type") == "Introduction" and h.get("added") for h in hidden or [])


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, markers_hidden FROM chapter_states WHERE intro_rule = 6")
    ).fetchall()
    keep = [row.id for row in rows if not _added_intro(row.markers_hidden)]
    for start in range(0, len(keep), 500):
        conn.execute(
            sa.text(
                "UPDATE chapter_states SET intro_rule = 7 WHERE id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": keep[start : start + 500]},
        )


def downgrade():
    pass
