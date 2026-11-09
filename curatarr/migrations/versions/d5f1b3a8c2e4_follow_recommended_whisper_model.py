"""Let installations that kept the old default model follow the recommended one.

The settings page used to save the transcription model with everything else, so
an installation that never chose one had "medium" (the default until 0.2.0rc101)
written down, and kept it when the default became large-v3-turbo. That saved
"medium" is removed, so the recommended model applies, unless transcription is
set to run on the processor, where turbo is slower than medium.

Revision ID: d5f1b3a8c2e4
Revises: c4e7a9d2f618
"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "d5f1b3a8c2e4"
down_revision = "c4e7a9d2f618"
branch_labels = None
depends_on = None

OLD_DEFAULT = "medium"


def _load(value):
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def _row(bind, key):
    return bind.execute(
        sa.text("SELECT id, value_json FROM app_settings WHERE key = :key"),
        {"key": key},
    ).first()


def upgrade():
    bind = op.get_bind()
    row = _row(bind, "chapter_settings")
    if not row:
        return
    stored = _load(row[1])
    semantic = dict(stored.get("semantic") or {})
    if semantic.get("model") != OLD_DEFAULT:
        return
    options = _row(bind, "chapter_options")
    if options and _load(options[1]).get("whisper_device") == "cpu":
        return
    del semantic["model"]
    stored["semantic"] = semantic
    bind.execute(
        sa.text(
            "UPDATE app_settings SET value_json = :value, updated_at = :now "
            "WHERE id = :id"
        ).bindparams(sa.bindparam("value", type_=sa.JSON)),
        {"value": stored, "now": datetime.now(UTC), "id": row[0]},
    )


def downgrade():
    pass
