"""Recheck episode identity under the stricter rules.

Earlier checks didn't record whether the matching episode was certain, or
positively confirm a file as its own episode, so they're checked again (except
flags someone dismissed or fixed, and audio flags).

Revision ID: e2b8c4f61a93
Revises: d7a3b9e1f620
"""

from alembic import op

revision = "e2b8c4f61a93"
down_revision = "d7a3b9e1f620"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE chapter_states SET identity_checked_at = NULL
        WHERE identity_checked_at IS NOT NULL
          AND (identity_issue IS NULL
               OR (CAST(identity_issue AS TEXT) LIKE '%"kind": "episode"%'
                   AND CAST(identity_issue AS TEXT) NOT LIKE '%dismissed%'))
        """
    )
    op.execute(
        """
        UPDATE chapter_states SET identity_issue = NULL
        WHERE identity_checked_at IS NULL
          AND CAST(identity_issue AS TEXT) LIKE '%"kind": "episode"%'
        """
    )


def downgrade():
    pass
