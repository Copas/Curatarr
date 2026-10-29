"""Blind choices between a title's current picture and a generated one
(0.2.0rc40).

Revision ID: d4b7e2a9c516
Revises: c8e4a1f7d392
"""

import sqlalchemy as sa
from alembic import op

revision = "d4b7e2a9c516"
down_revision = "c8e4a1f7d392"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "artwork_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("custom_poster_id", sa.String(length=36), nullable=False),
        sa.Column("winner", sa.String(length=10), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["artwork_candidates.id"]),
        sa.ForeignKeyConstraint(["custom_poster_id"], ["custom_posters.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artwork_preferences_candidate_id", "artwork_preferences", ["candidate_id"]
    )


def downgrade():
    op.drop_index(
        "ix_artwork_preferences_candidate_id", table_name="artwork_preferences"
    )
    op.drop_table("artwork_preferences")
