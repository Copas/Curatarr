"""Chapter enrichment state, and the arr-side file path of each media part.

Revision ID: e7c3a9f15d42
Revises: d2f6a8c41e93
"""

import sqlalchemy as sa
from alembic import op

revision = "e7c3a9f15d42"
down_revision = "d2f6a8c41e93"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_parts", sa.Column("file_path", sa.Text()))
    op.create_table(
        "chapter_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "media_part_id",
            sa.String(36),
            sa.ForeignKey("media_parts.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "media_identity_id",
            sa.String(36),
            sa.ForeignKey("media_identities.id"),
            nullable=False,
        ),
        sa.Column("chapter_status", sa.String(30), nullable=False),
        sa.Column("chapter_source", sa.String(20)),
        sa.Column("chapter_count", sa.Integer()),
        sa.Column("chapter_processed_at", sa.DateTime(timezone=True)),
        sa.Column("chapter_file_mtime", sa.Float()),
        sa.Column("chapter_file_size", sa.BigInteger()),
        sa.Column("chapter_generation_version", sa.Integer()),
        sa.Column("chapter_config_hash", sa.String(32)),
        sa.Column("chapter_error", sa.Text()),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("container", sa.String(60)),
        sa.Column("job_state", sa.String(20), nullable=False),
        sa.Column("requested_action", sa.String(20)),
        sa.Column("requested_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("written_fingerprint", sa.String(64)),
        sa.Column("written_chapters_json", sa.JSON()),
        sa.Column("original_chapters_xml", sa.Text()),
        sa.Column("last_preview_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_chapter_states_media_identity_id", "chapter_states", ["media_identity_id"]
    )
    op.create_index("ix_chapter_states_job_state", "chapter_states", ["job_state"])


def downgrade():
    op.drop_index("ix_chapter_states_job_state", "chapter_states")
    op.drop_index("ix_chapter_states_media_identity_id", "chapter_states")
    op.drop_table("chapter_states")
    op.drop_column("media_parts", "file_path")
