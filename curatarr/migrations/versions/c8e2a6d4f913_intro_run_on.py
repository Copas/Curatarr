"""Intro rule 8: an intro runs on while the season's titles show the same
pictures after the shared sound stops.

Only an episode with an intro can change (its end moves later), so episodes
with none (no intro from Intro Skipper, none added by Curatarr) are moved
straight to rule 8 instead of each being checked again.

Revision ID: c8e2a6d4f913
Revises: b6d3f8a1c427
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "c8e2a6d4f913"
down_revision = "b6d3f8a1c427"
branch_labels = None
depends_on = None


def _loaded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _has_intro(found, hidden) -> bool:
    found, hidden = _loaded(found) or {}, _loaded(hidden) or []
    if isinstance(found, dict) and found.get("theirs"):
        return True
    return any(isinstance(h, dict) and h.get("type") == "Introduction" for h in hidden)


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, intro_found, markers_hidden FROM chapter_states "
            "WHERE intro_rule = 7"
        )
    ).fetchall()
    none = [
        row.id for row in rows if not _has_intro(row.intro_found, row.markers_hidden)
    ]
    for start in range(0, len(none), 500):
        conn.execute(
            sa.text(
                "UPDATE chapter_states SET intro_rule = 8 WHERE id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": none[start : start + 500]},
        )


def downgrade():
    pass
