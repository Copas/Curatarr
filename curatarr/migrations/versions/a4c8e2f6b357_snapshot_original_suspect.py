"""Mark Leaving Soon snapshots whose saved original may be a badged picture.

Until this release, Jellyfin's image tag changing a while after an upload
read as "the artwork was replaced", so the badged picture itself was saved
as the original, again and again. Those snapshots are repaired by having
Jellyfin download the artwork afresh.

Revision ID: a4c8e2f6b357
Revises: f3b7d5e9a246
"""

import sqlalchemy as sa
from alembic import op

revision = "a4c8e2f6b357"
down_revision = "f3b7d5e9a246"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("poster_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "original_suspect",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("repair_requested_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE poster_snapshots SET original_suspect = TRUE
        WHERE EXISTS (
            SELECT 1 FROM lifecycle_actions
            WHERE lifecycle_actions.action_type = 'leaving_soon_reapplied'
              AND lifecycle_actions.idempotency_key
                  LIKE 'artwork-reapply:' || poster_snapshots.id || ':%'
        )
        """
    )


def downgrade():
    with op.batch_alter_table("poster_snapshots") as batch:
        batch.drop_column("repair_requested_at")
        batch.drop_column("original_suspect")
