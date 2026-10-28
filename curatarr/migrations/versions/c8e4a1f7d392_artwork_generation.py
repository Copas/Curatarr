"""Pictures made by AI from Curatarr: workers, jobs, the pictures asked for,
what was made, and ratings (0.2.0rc39).

Revision ID: c8e4a1f7d392
Revises: b7d5f3a9c042
"""

import sqlalchemy as sa
from alembic import op

revision = "c8e4a1f7d392"
down_revision = "b7d5f3a9c042"
branch_labels = None
depends_on = None


def _timed():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "artwork_workers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("capabilities", sa.JSON()),
        sa.Column("info", sa.JSON()),
        sa.Column("status", sa.JSON()),
        sa.Column("max_jobs", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        *_timed(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "artwork_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("media_ids", sa.JSON()),
        sa.Column("queued", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=100)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *_timed(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "artwork_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_identity_id", sa.String(length=36)),
        sa.Column("batch_id", sa.String(length=36)),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("accounts", sa.JSON()),
        sa.Column("kinds", sa.JSON(), nullable=False),
        sa.Column("engine", sa.String(length=100), nullable=False),
        sa.Column("worker_id", sa.String(length=36)),
        sa.Column("options", sa.JSON()),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_by", sa.String(length=100)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *_timed(),
        sa.ForeignKeyConstraint(["media_identity_id"], ["media_identities.id"]),
        sa.ForeignKeyConstraint(["batch_id"], ["artwork_batches.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["artwork_workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artwork_jobs_media_identity_id", "artwork_jobs", ["media_identity_id"]
    )
    op.create_index("ix_artwork_jobs_batch_id", "artwork_jobs", ["batch_id"])
    op.create_index("ix_artwork_jobs_state", "artwork_jobs", ["state"])
    op.create_table(
        "artwork_images",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("engine", sa.String(length=100)),
        sa.Column("worker_id", sa.String(length=36)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(length=500)),
        sa.Column("prompt", sa.Text()),
        sa.Column("task", sa.JSON()),
        sa.Column("feedback", sa.Text()),
        sa.Column("seed", sa.BigInteger()),
        sa.Column("candidate_id", sa.String(length=36)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        *_timed(),
        sa.ForeignKeyConstraint(["job_id"], ["artwork_jobs.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["artwork_workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artwork_images_job_id", "artwork_images", ["job_id"])
    op.create_index("ix_artwork_images_state", "artwork_images", ["state"])
    op.create_table(
        "artwork_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("image_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=512)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("has_alpha", sa.Boolean(), nullable=False),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("engine", sa.String(length=100)),
        sa.Column("worker_id", sa.String(length=36)),
        sa.Column("seed", sa.BigInteger()),
        sa.Column("prompt", sa.Text()),
        sa.Column("params", sa.JSON()),
        sa.Column("steps", sa.JSON()),
        sa.Column("metrics", sa.JSON()),
        sa.Column("duration_s", sa.Float()),
        sa.Column("peak_vram_mb", sa.Integer()),
        sa.Column("peak_ram_mb", sa.Integer()),
        sa.Column("decision", sa.String(length=20)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("feedback", sa.Text()),
        *_timed(),
        sa.ForeignKeyConstraint(["image_id"], ["artwork_images.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["artwork_workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artwork_candidates_image_id", "artwork_candidates", ["image_id"]
    )
    op.create_table(
        "artwork_ratings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("aspect", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=100)),
        *_timed(),
        sa.ForeignKeyConstraint(["candidate_id"], ["artwork_candidates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artwork_ratings_candidate_id", "artwork_ratings", ["candidate_id"]
    )
    op.create_table(
        "artwork_comparisons",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("left_id", sa.String(length=36), nullable=False),
        sa.Column("right_id", sa.String(length=36), nullable=False),
        sa.Column("winner", sa.String(length=10), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=100)),
        *_timed(),
        sa.ForeignKeyConstraint(["left_id"], ["artwork_candidates.id"]),
        sa.ForeignKeyConstraint(["right_id"], ["artwork_candidates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("artwork_comparisons")
    op.drop_index("ix_artwork_ratings_candidate_id", table_name="artwork_ratings")
    op.drop_table("artwork_ratings")
    op.drop_index("ix_artwork_candidates_image_id", table_name="artwork_candidates")
    op.drop_table("artwork_candidates")
    op.drop_index("ix_artwork_images_state", table_name="artwork_images")
    op.drop_index("ix_artwork_images_job_id", table_name="artwork_images")
    op.drop_table("artwork_images")
    op.drop_index("ix_artwork_jobs_state", table_name="artwork_jobs")
    op.drop_index("ix_artwork_jobs_batch_id", table_name="artwork_jobs")
    op.drop_index("ix_artwork_jobs_media_identity_id", table_name="artwork_jobs")
    op.drop_table("artwork_jobs")
    op.drop_table("artwork_batches")
    op.drop_table("artwork_workers")
