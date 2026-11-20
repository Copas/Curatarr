"""Intro rule 11: intros (and credits) whose redo gave up are placed again.

Rechecking, Curatarr swaps its marker back for Intro Skipper's original; when
Intro Skipper's own analysis had replaced it, the swap failed and the redo
gave up, yet the episode was counted as checked: it kept Intro Skipper's
uncorrected intro (86 episodes, FROM S4E5 among them). Episodes with an
intro are checked again (a missing marker of Curatarr's is placed again);
those without one move straight to rule 11.

Revision ID: b3e8d1f6a729
Revises: a1c5e9f2b704
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "b3e8d1f6a729"
down_revision = "a1c5e9f2b704"
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
            "WHERE intro_rule IN (7, 8, 9, 10)"
        )
    ).fetchall()
    none = [
        row.id for row in rows if not has_intro(row.intro_found, row.markers_hidden)
    ]
    for start in range(0, len(none), 500):
        conn.execute(
            sa.text(
                "UPDATE chapter_states SET intro_rule = 11 WHERE id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": none[start : start + 500]},
        )


def downgrade():
    pass
