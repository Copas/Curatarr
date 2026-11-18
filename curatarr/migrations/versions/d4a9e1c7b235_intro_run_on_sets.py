"""Intro rule 9: an intro doesn't run on over a set seen all through the
episodes (The Ricky Gervais Show's studio after its titles).

As for rule 8, only an episode with an intro can change, so those without
one move straight to rule 9; the rest are checked again, including any rule
8 already ran on.

Revision ID: d4a9e1c7b235
Revises: c8e2a6d4f913
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "d4a9e1c7b235"
down_revision = "c8e2a6d4f913"
branch_labels = None
depends_on = None


def _loaded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def has_intro(found, hidden) -> bool:
    found, hidden = _loaded(found) or {}, _loaded(hidden) or []
    if isinstance(found, dict) and found.get("theirs"):
        return True
    return any(isinstance(h, dict) and h.get("type") == "Introduction" for h in hidden)


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, intro_found, markers_hidden FROM chapter_states "
            "WHERE intro_rule IN (7, 8)"
        )
    ).fetchall()
    none = [
        row.id for row in rows if not has_intro(row.intro_found, row.markers_hidden)
    ]
    for start in range(0, len(none), 500):
        conn.execute(
            sa.text(
                "UPDATE chapter_states SET intro_rule = 9 WHERE id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": none[start : start + 500]},
        )


def downgrade():
    pass
