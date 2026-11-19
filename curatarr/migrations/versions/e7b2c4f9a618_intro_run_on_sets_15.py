"""Intro rule 10: a set counts from 16 places an episode, not 30 (The Ricky
Gervais Show's studio, at 16-27, still let intros run on).

As for rules 8 and 9, only an episode with an intro can change, so those
without one move straight to rule 10; the rest are checked again.

Revision ID: e7b2c4f9a618
Revises: d4a9e1c7b235
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "e7b2c4f9a618"
down_revision = "d4a9e1c7b235"
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
            "WHERE intro_rule IN (7, 8, 9)"
        )
    ).fetchall()
    none = [
        row.id for row in rows if not has_intro(row.intro_found, row.markers_hidden)
    ]
    for start in range(0, len(none), 500):
        conn.execute(
            sa.text(
                "UPDATE chapter_states SET intro_rule = 10 WHERE id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": none[start : start + 500]},
        )


def downgrade():
    pass
