"""Persistent observations, policy, and durable lifecycle work."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Index

from . import db


def utcnow() -> datetime:
    return datetime.now(UTC)


def uuid() -> str:
    return str(uuid4())


class Timed:
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AppSetting(db.Model, Timed):
    __tablename__ = "app_settings"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value_json = db.Column(db.JSON, nullable=False)
    is_secret = db.Column(db.Boolean, default=False, nullable=False)


class JobLease(db.Model):
    __tablename__ = "job_leases"
    scope = db.Column(db.String(150), primary_key=True)
    owner = db.Column(db.String(36), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)


class Integration(db.Model, Timed):
    __tablename__ = "integrations"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    kind = db.Column(db.String(20), unique=True, nullable=False)
    base_url = db.Column(db.String(512), nullable=False)
    secret_ref = db.Column(db.Text)
    detected_version = db.Column(db.String(80))
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    health_state = db.Column(db.String(20), default="unknown", nullable=False)
    last_health_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.Text)


class Library(db.Model, Timed):
    __tablename__ = "libraries"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    jellyfin_library_id = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    media_type = db.Column(db.String(20), nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    last_size_bytes = db.Column(db.BigInteger)
    last_scanned_at = db.Column(db.DateTime(timezone=True))
    policy = db.relationship("LibraryPolicy", backref="library", uselist=False)


class LibraryPolicy(db.Model, Timed):
    __tablename__ = "library_policies"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    library_id = db.Column(
        db.String(36), db.ForeignKey("libraries.id"), unique=True, nullable=False
    )
    policy_json = db.Column(db.JSON, default=dict, nullable=False)


class MediaIdentity(db.Model, Timed):
    __tablename__ = "media_identities"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    library_id = db.Column(db.String(36), db.ForeignKey("libraries.id"), nullable=False)
    media_type = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(512), nullable=False)
    sort_title = db.Column(db.String(512))
    jellyfin_id = db.Column(db.String(100), index=True)
    sonarr_id = db.Column(db.Integer, index=True)
    radarr_id = db.Column(db.Integer, index=True)
    tmdb_id = db.Column(db.Integer, index=True)
    tvdb_id = db.Column(db.Integer, index=True)
    imdb_id = db.Column(db.String(100), index=True)
    added_at = db.Column(db.DateTime(timezone=True))
    last_seen_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    missing_since = db.Column(db.DateTime(timezone=True))
    library = db.relationship("Library")
    override = db.relationship("TitleOverride", backref="media", uselist=False)
    parts = db.relationship("MediaPart", backref="media", cascade="all, delete-orphan")


class TitleOverride(db.Model, Timed):
    __tablename__ = "title_overrides"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), unique=True, nullable=False
    )
    override_json = db.Column(db.JSON, default=dict, nullable=False)


class MediaPart(db.Model, Timed):
    __tablename__ = "media_parts"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False
    )
    kind = db.Column(db.String(20), nullable=False)
    season_number = db.Column(db.Integer)
    episode_number = db.Column(db.Integer)
    jellyfin_id = db.Column(db.String(100), index=True)
    sonarr_episode_id = db.Column(db.Integer)
    arr_file_id = db.Column(db.Integer)
    size_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    has_file = db.Column(db.Boolean, default=False, nullable=False)
    air_date = db.Column(db.DateTime(timezone=True))
    acquired_at = db.Column(db.DateTime(timezone=True))
    __table_args__ = (
        db.UniqueConstraint(
            "media_identity_id", "kind", "season_number", "episode_number"
        ),
    )


class UserMediaState(db.Model, Timed):
    __tablename__ = "user_media_state"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False
    )
    jellyfin_user_id = db.Column(db.String(100), nullable=False)
    last_played_at = db.Column(db.DateTime(timezone=True))
    completed_episode_count = db.Column(db.Integer, default=0, nullable=False)
    favorite = db.Column(db.Boolean, default=False, nullable=False)
    __table_args__ = (db.UniqueConstraint("media_identity_id", "jellyfin_user_id"),)


class EpisodeUserState(db.Model, Timed):
    __tablename__ = "episode_user_state"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_part_id = db.Column(
        db.String(36), db.ForeignKey("media_parts.id"), nullable=False
    )
    jellyfin_user_id = db.Column(db.String(100), nullable=False)
    played = db.Column(db.Boolean, default=False, nullable=False)
    last_played_at = db.Column(db.DateTime(timezone=True))
    playback_position_ticks = db.Column(db.BigInteger, default=0, nullable=False)
    favorite = db.Column(db.Boolean, default=False, nullable=False)
    __table_args__ = (db.UniqueConstraint("media_part_id", "jellyfin_user_id"),)


class AcquisitionState(db.Model, Timed):
    __tablename__ = "acquisition_states"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), unique=True, nullable=False
    )
    state = db.Column(db.String(30), default="DORMANT", nullable=False)
    highest_demonstrated_season = db.Column(db.Integer)
    target_next_season = db.Column(db.Integer)
    plan_revision = db.Column(db.Integer, default=0, nullable=False)
    last_triggered_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.Text)


ACTIVE_CANDIDATE_STATES = (
    "ELIGIBLE",
    "REVIEW",
    "LEAVING_SOON",
    "SNOOZED",
    "APPROVED",
    "EXECUTING",
)


class PurgeCandidate(db.Model, Timed):
    __tablename__ = "purge_candidates"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False
    )
    state = db.Column(db.String(30), nullable=False)
    candidate_revision = db.Column(db.Integer, default=1, nullable=False)
    reason_code = db.Column(db.String(50), nullable=False)
    reason_text = db.Column(db.Text, nullable=False)
    reclaimable_bytes = db.Column(db.BigInteger, nullable=False)
    score = db.Column(db.Float)
    score_detail_json = db.Column(db.JSON)
    eligible_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    last_activity_snapshot = db.Column(db.DateTime(timezone=True))
    scheduled_delete_at = db.Column(db.DateTime(timezone=True))
    snooze_until = db.Column(db.DateTime(timezone=True))
    approved_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    media = db.relationship("MediaIdentity")


Index(
    "uq_active_candidate",
    PurgeCandidate.media_identity_id,
    unique=True,
    sqlite_where=PurgeCandidate.state.in_(ACTIVE_CANDIDATE_STATES),
    postgresql_where=PurgeCandidate.state.in_(ACTIVE_CANDIDATE_STATES),
)


class PosterSnapshot(db.Model, Timed):
    __tablename__ = "poster_snapshots"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False
    )
    jellyfin_item_id = db.Column(db.String(100), nullable=False)
    original_image_tag = db.Column(db.String(100))
    badged_image_tag = db.Column(db.String(100))
    original_image_bytes_path = db.Column(db.String(512))
    badged_image_bytes_path = db.Column(db.String(512))
    candidate_id = db.Column(db.String(36), db.ForeignKey("purge_candidates.id"))
    active = db.Column(db.Boolean, default=False, nullable=False)
    restored_at = db.Column(db.DateTime(timezone=True))


class LifecycleEvent(db.Model, Timed):
    __tablename__ = "lifecycle_events"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    external_event_id = db.Column(db.String(128), unique=True)
    source = db.Column(db.String(30), nullable=False)
    event_type = db.Column(db.String(40), nullable=False)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    media_identity_id = db.Column(db.String(36), db.ForeignKey("media_identities.id"))
    jellyfin_user_id = db.Column(db.String(100))
    payload_hash = db.Column(db.String(64), nullable=False)
    normalized_json = db.Column(db.JSON, nullable=False)
    processed_at = db.Column(db.DateTime(timezone=True))


class LifecycleAction(db.Model, Timed):
    __tablename__ = "lifecycle_actions"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    idempotency_key = db.Column(db.String(255), unique=True, nullable=False)
    action_type = db.Column(db.String(50), nullable=False)
    state = db.Column(db.String(30), default="PENDING", nullable=False)
    media_identity_id = db.Column(db.String(36), db.ForeignKey("media_identities.id"))
    candidate_id = db.Column(db.String(36), db.ForeignKey("purge_candidates.id"))
    event_id = db.Column(db.String(36), db.ForeignKey("lifecycle_events.id"))
    reason_text = db.Column(db.Text, nullable=False)
    payload_json = db.Column(db.JSON, default=dict, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    next_retry_at = db.Column(db.DateTime(timezone=True))
    started_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.Text)


class WatchStateSnapshot(db.Model, Timed):
    __tablename__ = "watch_state_snapshots"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False
    )
    jellyfin_user_id = db.Column(db.String(100), nullable=False)
    provider_key = db.Column(db.String(100), nullable=False)
    season_number = db.Column(db.Integer)
    episode_number = db.Column(db.Integer)
    played = db.Column(db.Boolean, nullable=False)
    playback_position_ticks = db.Column(db.BigInteger, default=0, nullable=False)
    date_played = db.Column(db.DateTime(timezone=True))
    restored_at = db.Column(db.DateTime(timezone=True))
