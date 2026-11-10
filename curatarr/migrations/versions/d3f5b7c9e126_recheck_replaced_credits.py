"""Check again the episodes whose credits marker Curatarr replaced (0.2.0rc134).

When the marker being replaced had already gone, Curatarr took that as a
failure and removed its own new marker too, leaving some episodes with no
credits marker. Those episodes are checked again; one with none gets one.

Revision ID: d3f5b7c9e126
Revises: c7e2a4b6d815
"""

from alembic import op

revision = "d3f5b7c9e126"
down_revision = "c7e2a4b6d815"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE chapter_states SET credits_rule = NULL "
        "WHERE markers_hidden LIKE '%replaced_by%'"
    )


def downgrade():
    pass
