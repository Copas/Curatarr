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


class MetricCounter(db.Model):
    __tablename__ = "metric_counters"
    name = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.BigInteger, default=0, nullable=False)


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

    @property
    def managed(self):
        """Only TV and movie libraries map to Sonarr/Radarr; others are not managed."""
        return self.media_type in {"tv", "movies"}


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
    # Sonarr/Radarr's own monitored flag; False means "download nothing".
    arr_monitored = db.Column(db.Boolean)
    series_type = db.Column(db.String(20))  # Sonarr: standard, daily, anime
    # Set by "Reset to minimum": viewing before this time no longer drives acquisition.
    viewing_reset_at = db.Column(db.DateTime(timezone=True))
    # From Sonarr/Radarr, for seasonal shelves (curation.py).
    genres = db.Column(db.JSON)
    keywords = db.Column(db.JSON)
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
    # The file's path as Sonarr/Radarr report it (see chapter path mappings).
    file_path = db.Column(db.Text)
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
    # When it was favourited, if Curatarr saw it happen (favourite ribbons'
    # "NEW"); None for favourites older than Curatarr's first look.
    favorite_since = db.Column(db.DateTime(timezone=True))
    # A film: how many times this person has played it. A show: how many of
    # its episodes they have played more than once.
    play_count = db.Column(db.Integer, default=0, nullable=False)
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
    favorite_since = db.Column(db.DateTime(timezone=True))
    __table_args__ = (db.UniqueConstraint("media_part_id", "jellyfin_user_id"),)


class MediaFlag(db.Model, Timed):
    """A flag set on a film or show, one of its seasons, or one episode or
    file: "leave_after_watched" (Leaving Soon once it's been watched) or
    "never_compress" (an external transcoder leaves its files alone)."""

    __tablename__ = "media_flags"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False, index=True
    )
    season_number = db.Column(db.Integer)  # a season of a show
    media_part_id = db.Column(db.String(36), db.ForeignKey("media_parts.id"))
    flag = db.Column(db.String(30), nullable=False)
    # leave_after_watched: whose watch counts (None: anyone's), and for a show
    # or season, "each" episode as it's watched or "all" once the last is.
    jellyfin_user_id = db.Column(db.String(100))
    mode = db.Column(db.String(20))
    set_by = db.Column(db.String(100))  # who set it, for History


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
    # Only these episodes (MediaPart ids) leave, not the show: watched ones
    # flagged Leave after watched. None: the whole title, as the rules say.
    target_part_ids = db.Column(db.JSON(none_as_null=True))
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
    badge_label = db.Column(db.String(40))  # the banner's words, e.g. days left
    # Primary (poster), Thumb (wide card) or Backdrop: each badged separately.
    image_type = db.Column(
        db.String(20), nullable=False, default="Primary", server_default="Primary"
    )
    # The saved original may itself be badged (an old bug): restore and repair
    # by having Jellyfin download the artwork afresh instead.
    original_suspect = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    repair_requested_at = db.Column(db.DateTime(timezone=True))
    candidate_id = db.Column(db.String(36), db.ForeignKey("purge_candidates.id"))
    active = db.Column(db.Boolean, default=False, nullable=False)
    restored_at = db.Column(db.DateTime(timezone=True))


class CustomPoster(db.Model, Timed):
    """A picture someone chose for a title (custom_posters.py): its poster or
    another of its pictures (``image_type``, Jellyfin's: Primary, Thumb,
    Backdrop, Logo or Banner). Only one is active per title, type and account;
    ``jellyfin_user_id`` None means everyone (the picture Jellyfin itself has)."""

    __tablename__ = "custom_posters"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False, index=True
    )
    jellyfin_user_id = db.Column(db.String(100))
    image_type = db.Column(
        db.String(20), nullable=False, default="Primary", server_default="Primary"
    )
    image_path = db.Column(db.String(512), nullable=False)
    # The poster it replaced; None when that wasn't known (it was already
    # showing when imported), so restoring takes the provider's poster.
    original_path = db.Column(db.String(512))
    source = db.Column(db.String(255))  # "Uploaded" or the imported file's name
    active = db.Column(db.Boolean, default=True, nullable=False)
    applied_at = db.Column(db.DateTime(timezone=True))
    restored_at = db.Column(db.DateTime(timezone=True))
    media = db.relationship("MediaIdentity")


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


class ChapterState(db.Model, Timed):
    """Chapter enrichment for one media file (see enrichment.py).

    ``chapter_file_size``/``chapter_file_mtime`` describe the file as Curatarr
    last left it, so an unchanged file is never processed twice. A file counts
    as carrying Curatarr's chapters only while its chapters still match
    ``written_fingerprint``.
    """

    __tablename__ = "chapter_states"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_part_id = db.Column(
        db.String(36), db.ForeignKey("media_parts.id"), unique=True, nullable=False
    )
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), nullable=False, index=True
    )
    chapter_status = db.Column(db.String(30), default="not_processed", nullable=False)
    chapter_source = db.Column(db.String(20))
    chapter_count = db.Column(db.Integer)
    chapter_processed_at = db.Column(db.DateTime(timezone=True))
    chapter_file_mtime = db.Column(db.Float)
    chapter_file_size = db.Column(db.BigInteger)
    chapter_generation_version = db.Column(db.Integer)
    chapter_config_hash = db.Column(db.String(32))
    chapter_error = db.Column(db.Text)
    # For an unexpected error: where it happened (the end of the traceback).
    chapter_error_detail = db.Column(db.Text)
    duration_seconds = db.Column(db.Float)
    container = db.Column(db.String(60))
    # Work queue: idle, queued, running. ``requested_action`` is what to run.
    job_state = db.Column(db.String(20), default="idle", nullable=False, index=True)
    requested_action = db.Column(db.String(20))
    requested_at = db.Column(db.DateTime(timezone=True))
    attempts = db.Column(db.Integer, default=0, nullable=False)
    next_attempt_at = db.Column(db.DateTime(timezone=True))
    written_fingerprint = db.Column(db.String(64))
    written_chapters_json = db.Column(db.JSON)
    # Chapters that were in the file before Curatarr replaced them, as XML.
    original_chapters_xml = db.Column(db.Text)
    last_preview_json = db.Column(db.JSON)
    # Generated English subtitles (a .en.Curatarr.srt beside the video). The
    # file is Curatarr's only while its contents still hash to the fingerprint.
    subtitle_status = db.Column(db.String(30))
    subtitle_path = db.Column(db.Text)
    subtitle_fingerprint = db.Column(db.String(64))
    subtitle_processed_at = db.Column(db.DateTime(timezone=True))
    subtitle_error = db.Column(db.Text)
    # Whether an English subtitle file beside the video, not Curatarr's, lines
    # up with the speech, and what was done about it (subtitle_sync.py). Keyed
    # by the file's name, size and time, so it's checked again if it changes.
    subtitle_sync = db.Column(db.JSON)
    # Transcript cache keys, so other episodes of the series can be read back
    # when looking for a recap (see recaps.py).
    transcript_key = db.Column(db.String(32))
    opening_key = db.Column(db.String(32))
    recap_checked_at = db.Column(db.DateTime(timezone=True))
    recap_start = db.Column(db.Float)
    recap_end = db.Column(db.Float)
    recap_source = db.Column(db.String(30))
    recap_segment_id = db.Column(db.String(40))  # Intro Skipper's id for ours
    preview_checked_at = db.Column(db.DateTime(timezone=True))
    preview_start = db.Column(db.Float)
    preview_end = db.Column(db.Float)
    preview_source = db.Column(db.String(30))
    preview_segment_id = db.Column(db.String(40))
    # Intro Skipper markers Curatarr hid as plainly wrong, with the reasons.
    markers_checked_at = db.Column(db.DateTime(timezone=True))
    markers_hidden = db.Column(db.JSON)
    # Whether the transcript fits this episode (see identity.py).
    identity_checked_at = db.Column(db.DateTime(timezone=True))
    identity_issue = db.Column(db.JSON)
    # Whether a film made in another language has real English audio (dubs.py).
    dub_checked_at = db.Column(db.DateTime(timezone=True))
    dub_file_size = db.Column(db.BigInteger)  # the file that was checked
    dub_result = db.Column(db.JSON)
    # Burned-in subtitles and signs of a broken file (filecheck.py).
    file_checked_at = db.Column(db.DateTime(timezone=True))
    file_check_size = db.Column(db.BigInteger)  # the file that was checked
    file_check = db.Column(db.JSON)
    file_problem = db.Column(db.Boolean, index=True)  # something to act on
    # What's said, counted for Library → Fun facts (trivia.py).
    talk_stats = db.Column(db.JSON)
    talk_stats_key = db.Column(db.String(32))  # the transcript counted
    # A TV-friendly audio track added beside DTS/TrueHD/FLAC (compat_audio.py).
    compat_audio_size = db.Column(db.BigInteger)  # the file that was checked
    compat_audio = db.Column(db.JSON)
    # Whether the file plays a commentary or audio description by default.
    default_audio_size = db.Column(db.BigInteger)  # the file that was checked
    default_audio = db.Column(db.JSON)
    # Seek index repair for files Roku players couldn't seek in (seek_repair.py).
    seek_layout_size = db.Column(db.BigInteger)  # the file that was checked
    seek_layout = db.Column(db.JSON)
    part = db.relationship("MediaPart")
    media = db.relationship("MediaIdentity")


class ArtworkWorker(db.Model, Timed):
    """A computer that makes pictures for Curatarr (artwork_jobs.py). It asks
    for work with its token; only the token's hash is kept."""

    __tablename__ = "artwork_workers"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    name = db.Column(db.String(100), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    # What it can make: [{"engine", "label", "kinds", "references", "external"}]
    capabilities = db.Column(db.JSON)
    info = db.Column(db.JSON)  # host, processor, graphics card, memory
    status = db.Column(db.JSON)  # each backend's readiness, as last reported
    max_jobs = db.Column(db.Integer, default=1, nullable=False)
    last_seen_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.Text)


class ArtworkBatch(db.Model, Timed):
    """Pictures for many titles at once (a library's missing artwork), or a
    benchmark: its titles are queued a few jobs at a time."""

    __tablename__ = "artwork_batches"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    name = db.Column(db.String(200), nullable=False)
    purpose = db.Column(db.String(20), default="library", nullable=False)
    spec = db.Column(db.JSON, nullable=False)  # what was asked for
    state = db.Column(db.String(20), default="active", nullable=False)
    media_ids = db.Column(db.JSON)  # the titles, in order
    queued = db.Column(db.Integer, default=0, nullable=False)  # how many so far
    created_by = db.Column(db.String(100))
    finished_at = db.Column(db.DateTime(timezone=True))


class ArtworkJob(db.Model, Timed):
    """Pictures asked for one title (or one benchmark scenario): one
    ArtworkImage per type, each reviewed on its own."""

    __tablename__ = "artwork_jobs"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    media_identity_id = db.Column(
        db.String(36), db.ForeignKey("media_identities.id"), index=True
    )
    batch_id = db.Column(db.String(36), db.ForeignKey("artwork_batches.id"), index=True)
    purpose = db.Column(db.String(20), default="library", nullable=False)
    accounts = db.Column(db.JSON)  # Jellyfin accounts it's for; empty: everyone
    kinds = db.Column(db.JSON, nullable=False)
    engine = db.Column(db.String(100), default="auto", nullable=False)
    worker_id = db.Column(db.String(36), db.ForeignKey("artwork_workers.id"))
    options = db.Column(db.JSON)  # the look, references, title details
    priority = db.Column(db.Integer, default=0, nullable=False)
    state = db.Column(db.String(20), default="pending", nullable=False, index=True)
    error = db.Column(db.Text)
    created_by = db.Column(db.String(100))
    finished_at = db.Column(db.DateTime(timezone=True))
    media = db.relationship("MediaIdentity")
    batch = db.relationship("ArtworkBatch", backref="jobs")
    worker = db.relationship("ArtworkWorker")
    images = db.relationship(
        "ArtworkImage", backref="job", order_by="ArtworkImage.position"
    )


class ArtworkImage(db.Model, Timed):
    """One picture asked for (a poster, a wide tile…) and its progress. Each
    try leaves an ArtworkCandidate for review."""

    __tablename__ = "artwork_images"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    job_id = db.Column(
        db.String(36), db.ForeignKey("artwork_jobs.id"), nullable=False, index=True
    )
    kind = db.Column(db.String(20), nullable=False)
    position = db.Column(db.Integer, default=0, nullable=False)
    state = db.Column(db.String(20), default="pending", nullable=False, index=True)
    engine = db.Column(db.String(100))  # the one it ran on
    worker_id = db.Column(db.String(36), db.ForeignKey("artwork_workers.id"))
    attempts = db.Column(db.Integer, default=0, nullable=False)
    lease_expires_at = db.Column(db.DateTime(timezone=True))
    not_before = db.Column(db.DateTime(timezone=True))
    progress = db.Column(db.Integer, default=0, nullable=False)
    message = db.Column(db.String(500))
    prompt = db.Column(db.Text)  # the last one sent
    task = db.Column(db.JSON)  # the rest of it, with its references
    feedback = db.Column(db.Text)  # for the next try
    seed = db.Column(db.BigInteger)
    candidate_id = db.Column(db.String(36))  # the one approved
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    error = db.Column(db.Text)
    worker = db.relationship("ArtworkWorker")
    candidates = db.relationship(
        "ArtworkCandidate", backref="image", order_by="ArtworkCandidate.version"
    )


class ArtworkCandidate(db.Model, Timed):
    """A picture a worker made, waiting in staging until it's approved
    (put on through custom_posters) or rejected."""

    __tablename__ = "artwork_candidates"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    image_id = db.Column(
        db.String(36), db.ForeignKey("artwork_images.id"), nullable=False, index=True
    )
    version = db.Column(db.Integer, default=1, nullable=False)
    path = db.Column(db.String(512))  # None once retention has removed it
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    has_alpha = db.Column(db.Boolean, default=False, nullable=False)
    size_bytes = db.Column(db.Integer)
    engine = db.Column(db.String(100))
    worker_id = db.Column(db.String(36), db.ForeignKey("artwork_workers.id"))
    seed = db.Column(db.BigInteger)
    prompt = db.Column(db.Text)
    params = db.Column(db.JSON)  # model, workflow, steps, sizes, references
    steps = db.Column(db.JSON)  # what was done to it after generation
    metrics = db.Column(db.JSON)  # measured checks, each marked how reliable
    duration_s = db.Column(db.Float)
    peak_vram_mb = db.Column(db.Integer)
    peak_ram_mb = db.Column(db.Integer)
    decision = db.Column(db.String(20))  # approved, rejected, superseded
    decided_at = db.Column(db.DateTime(timezone=True))
    feedback = db.Column(db.Text)
    worker = db.relationship("ArtworkWorker")


class ArtworkRating(db.Model, Timed):
    """A person's score (1–5) for one aspect of a picture: overall, likeness,
    lettering or how it fits its set."""

    __tablename__ = "artwork_ratings"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    candidate_id = db.Column(
        db.String(36),
        db.ForeignKey("artwork_candidates.id"),
        nullable=False,
        index=True,
    )
    aspect = db.Column(db.String(20), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    created_by = db.Column(db.String(100))


class ArtworkComparison(db.Model, Timed):
    """A blind choice between two pictures made for the same thing."""

    __tablename__ = "artwork_comparisons"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    left_id = db.Column(
        db.String(36), db.ForeignKey("artwork_candidates.id"), nullable=False
    )
    right_id = db.Column(
        db.String(36), db.ForeignKey("artwork_candidates.id"), nullable=False
    )
    winner = db.Column(db.String(10), nullable=False)  # left, right, tie
    notes = db.Column(db.Text)
    created_by = db.Column(db.String(100))


class ArtworkPreference(db.Model, Timed):
    """A blind choice between a title's picture as it is (CustomPoster) and
    one made by a worker for the same title and type."""

    __tablename__ = "artwork_preferences"
    id = db.Column(db.String(36), primary_key=True, default=uuid)
    candidate_id = db.Column(
        db.String(36),
        db.ForeignKey("artwork_candidates.id"),
        nullable=False,
        index=True,
    )
    custom_poster_id = db.Column(
        db.String(36), db.ForeignKey("custom_posters.id"), nullable=False
    )
    winner = db.Column(db.String(10), nullable=False)  # new, current, tie
    notes = db.Column(db.Text)
    created_by = db.Column(db.String(100))
    candidate = db.relationship("ArtworkCandidate")
    current = db.relationship("CustomPoster")
