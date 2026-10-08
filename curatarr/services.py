"""Application services for discovery, events, and lifecycle decisions."""

import hashlib
import json
import time
from datetime import UTC, datetime

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from . import db
from .integrations import CLIENTS, IntegrationError
from .models import (
    ACTIVE_CANDIDATE_STATES,
    AcquisitionState,
    AppSetting,
    EpisodeUserState,
    Integration,
    Library,
    LibraryPolicy,
    LifecycleAction,
    LifecycleEvent,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    UserMediaState,
    WatchStateSnapshot,
    utcnow,
)
from .observability import increment, log_operation
from .policy import (
    acquisition_seasons,
    effective_policy,
)


def setting(key, default=None):
    row = db.session.query(AppSetting).filter_by(key=key).first()
    if not row:
        return default
    if row.is_secret:
        from .secrets import decrypt_secret

        return decrypt_secret(row.value_json)
    return row.value_json


def set_setting(key, value):
    is_secret = key == "webhook_token"
    if is_secret and value is not None:
        from .secrets import encrypt_secret

        value = encrypt_secret(value)
    row = db.session.query(AppSetting).filter_by(key=key).first()
    if not row:
        row = AppSetting(key=key, value_json=value, is_secret=is_secret)
        db.session.add(row)
    else:
        row.value_json = value
        row.is_secret = is_secret
    db.session.commit()


def client(kind):
    if current_app.config["DEMO_MODE"]:
        from .demo import demo_client

        return demo_client(kind)
    row = db.session.query(Integration).filter_by(kind=kind, enabled=True).first()
    if not row or not row.secret_ref:
        raise IntegrationError(f"{kind} is not configured")
    from .secrets import decrypt_secret

    try:
        key = decrypt_secret(row.secret_ref)
    except ValueError as exc:
        raise IntegrationError(f"{kind} credential is unavailable") from exc
    return CLIENTS[kind](row.base_url, key)


def check_integration(kind):
    row = db.session.query(Integration).filter_by(kind=kind).first()
    if not row:
        return "unconfigured"
    if not row.secret_ref and not current_app.config["DEMO_MODE"]:
        # First-run Jellyfin sign-in saves the URL before any API key exists.
        row.health_state = "unconfigured"
        row.last_error = None
        db.session.commit()
        return row.health_state
    try:
        status = client(kind).health()
        row.health_state = "healthy"
        row.detected_version = status.get("Version") or status.get("version")
        row.last_error = None
    except IntegrationError:
        row.health_state = "unhealthy"
        row.last_error = "Connection or API request failed"
    row.last_health_at = utcnow()
    db.session.commit()
    return row.health_state


def resolved_policy(media):
    global_values = setting("global_policy", {})
    library_values = media.library.policy.policy_json if media.library.policy else {}
    title_values = media.override.override_json if media.override else {}
    return effective_policy(
        media.media_type, global_values, library_values, title_values
    )


def audit(
    action_type,
    media_id,
    reason,
    key,
    *,
    payload=None,
    state="SUCCEEDED",
    candidate_id=None,
    event_id=None,
):
    existing = db.session.query(LifecycleAction).filter_by(idempotency_key=key).first()
    if existing:
        return existing
    action = LifecycleAction(
        idempotency_key=key,
        action_type=action_type,
        state=state,
        media_identity_id=media_id,
        candidate_id=candidate_id,
        event_id=event_id,
        reason_text=reason,
        payload_json=payload or {},
        completed_at=utcnow() if state == "SUCCEEDED" else None,
    )
    db.session.add(action)
    db.session.flush()
    return action


def _as_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(value)
    except (ValueError, AttributeError):
        return None


def _providers(item):
    ids = item.get("ProviderIds") or item.get("providerIds") or {}

    def integer(key):
        try:
            return int(ids.get(key) or ids.get(key.lower()))
        except (ValueError, TypeError):
            return None

    return integer("Tmdb"), integer("Tvdb"), ids.get("Imdb") or ids.get("imdb")


def _match_media(library, media_type, item):
    external_id = item.get("Id") or item.get("id")
    tmdb, tvdb, imdb = _providers(item)
    base = db.session.query(MediaIdentity).filter_by(
        library_id=library.id, media_type=media_type
    )
    media = (
        base.filter_by(jellyfin_id=str(external_id)).first() if external_id else None
    )
    if not media:
        clauses = []
        if tmdb:
            clauses.append(MediaIdentity.tmdb_id == tmdb)
        if tvdb:
            clauses.append(MediaIdentity.tvdb_id == tvdb)
        if imdb:
            clauses.append(MediaIdentity.imdb_id == imdb)
        if clauses:
            matches = base.filter(or_(*clauses)).limit(2).all()
            media = matches[0] if len(matches) == 1 else None
    if not media:
        media = MediaIdentity(
            library_id=library.id, media_type=media_type, title=item["Name"]
        )
        db.session.add(media)
    media.title = item.get("Name") or media.title
    media.jellyfin_id = str(external_id) if external_id else media.jellyfin_id
    media.tmdb_id = tmdb or media.tmdb_id
    media.tvdb_id = tvdb or media.tvdb_id
    media.imdb_id = imdb or media.imdb_id
    media.added_at = _as_datetime(item.get("DateCreated")) or media.added_at
    media.last_seen_at = utcnow()
    media.missing_since = None
    db.session.flush()
    return media


def _upsert_part(media, kind, season, episode, item):
    part = (
        db.session.query(MediaPart)
        .filter_by(
            media_identity_id=media.id,
            kind=kind,
            season_number=season,
            episode_number=episode,
        )
        .first()
    )
    if not part:
        part = MediaPart(
            media_identity_id=media.id,
            kind=kind,
            season_number=season,
            episode_number=episode,
        )
        db.session.add(part)
    part.jellyfin_id = str(item.get("Id")) if item.get("Id") else part.jellyfin_id
    has_file = bool(item.get("HasFile", part.has_file))
    # Sonarr/Radarr report when each file was added; that is the acquisition
    # time. Using the discovery time instead made a whole existing library look
    # newly acquired, deferring grace and inactivity for everything at once.
    added = _as_datetime(item.get("AddedAt")) if has_file else None
    if added:
        part.acquired_at = added
    elif has_file and not part.has_file:
        part.acquired_at = utcnow()
    part.has_file = has_file
    size = item.get("Size")
    if size is not None:
        part.size_bytes = size if type(size) is int and size >= 0 else 0
    db.session.flush()
    return part


def _library_type(collection_type):
    return {"tvshows": "tv", "movies": "movies"}.get(
        (collection_type or "").lower(), "other"
    )


def discover():
    """Read Jellyfin and arr state; persist stable identity and file observations."""
    jellyfin = client("jellyfin")
    sonarr = client("sonarr")
    radarr = client("radarr")
    libraries = jellyfin.libraries()
    sonarr_series = sonarr.series()
    radarr_movies = radarr.movies()
    series_by_tvdb = {str(s.get("tvdbId")): s for s in sonarr_series if s.get("tvdbId")}
    movies_by_tmdb = {str(m.get("tmdbId")): m for m in radarr_movies if m.get("tmdbId")}
    seen = set()
    for source in libraries:
        external_id = str(source.get("ItemId") or source.get("Id"))
        library = (
            db.session.query(Library).filter_by(jellyfin_library_id=external_id).first()
        )
        if not library:
            library = Library(
                jellyfin_library_id=external_id,
                name=source.get("Name", "Library"),
                media_type=_library_type(source.get("CollectionType")),
            )
            db.session.add(library)
            db.session.flush()
        library.name = source.get("Name", library.name)
        library.media_type = _library_type(source.get("CollectionType"))
        if not library.enabled or library.media_type not in {"tv", "movies"}:
            continue
        offset = 0
        observed_episodes = []
        while True:
            page = jellyfin.items(external_id, offset, 100)
            entries = page.get("Items", [])
            for item in entries:
                if item.get("Type") not in {"Series", "Movie", "Episode"}:
                    continue
                if item["Type"] == "Episode":
                    observed_episodes.append(item)
                    continue
                media_type = "series" if item["Type"] == "Series" else "movie"
                media = _match_media(library, media_type, item)
                seen.add(media.id)
                if media_type == "series" and media.tvdb_id:
                    match = series_by_tvdb.get(str(media.tvdb_id))
                    if match:
                        media.sonarr_id = match["id"]
                        media.arr_monitored = match.get("monitored")
                        media.series_type = match.get("seriesType")
                        episode_files = {
                            entry["id"]: entry
                            for entry in sonarr.episode_files(media.sonarr_id)
                        }
                        file_sizes = {
                            file_id: entry.get("size", 0)
                            for file_id, entry in episode_files.items()
                        }
                        for episode in sonarr.episodes(media.sonarr_id):
                            file_entry = episode_files.get(episode.get("episodeFileId"))
                            part = _upsert_part(
                                media,
                                "episode",
                                episode.get("seasonNumber"),
                                episode.get("episodeNumber"),
                                {
                                    "HasFile": episode.get("hasFile", False),
                                    "AddedAt": (file_entry or {}).get("dateAdded"),
                                },
                            )
                            part.sonarr_episode_id = episode.get("id")
                            part.arr_file_id = episode.get("episodeFileId") or None
                            size = file_sizes.get(part.arr_file_id, 0)
                            part.size_bytes = (
                                size if type(size) is int and size >= 0 else 0
                            )
                            part.air_date = _as_datetime(episode.get("airDateUtc"))
                if media_type == "movie" and media.tmdb_id:
                    match = movies_by_tmdb.get(str(media.tmdb_id))
                    if match:
                        media.radarr_id = match["id"]
                        media.arr_monitored = match.get("monitored")
                        file_data = match.get("movieFile") or {}
                        _upsert_part(
                            media,
                            "movie_file",
                            None,
                            None,
                            {
                                "HasFile": match.get("hasFile", False),
                                "AddedAt": file_data.get("dateAdded"),
                                "Size": file_data.get(
                                    "size", match.get("sizeOnDisk", 0)
                                ),
                            },
                        )
            offset += len(entries)
            if not entries or offset >= page.get("TotalRecordCount", offset):
                break
        for item in observed_episodes:
            media = (
                db.session.query(MediaIdentity)
                .filter_by(library_id=library.id, jellyfin_id=str(item.get("SeriesId")))
                .first()
            )
            if media:
                _upsert_part(
                    media,
                    "episode",
                    item.get("ParentIndexNumber"),
                    item.get("IndexNumber"),
                    item,
                )
        library.last_scanned_at = utcnow()
    for media in db.session.query(MediaIdentity).all():
        if media.id not in seen and media.missing_since is None:
            media.missing_since = utcnow()
    for library in db.session.query(Library).all():
        library.last_size_bytes = (
            db.session.query(func.sum(MediaPart.size_bytes))
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(
                MediaIdentity.library_id == library.id, MediaPart.has_file.is_(True)
            )
            .scalar()
            or 0
        )
    db.session.commit()
    return len(libraries)


def _flag(value):
    """Booleans from JSON or from Jellyfin Webhook plugin templates ("True", "")."""
    if isinstance(value, bool) or value is None:
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _jellyfin_id(value):
    """Jellyfin GUIDs appear with or without dashes; use the API's dashless form."""
    if value is None:
        return None
    text = str(value).strip()
    compact = text.replace("-", "").lower()
    if len(compact) == 32 and all(char in "0123456789abcdef" for char in compact):
        return compact
    return text or None


def _first(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


# UserDataSaved reasons that mean the viewer finished or marked the item watched.
PLAYED_SAVE_REASONS = {"playbackfinished", "toggleplayed"}


class UnsupportedWebhookEvent(ValueError):
    """A well-formed notification of a type Curatarr does not act on."""


def normalize_event(payload):
    """Accept the Jellyfin Webhook plugin's fields and Curatarr's canonical form.

    The plugin can send all of its properties (native JSON types) or a template
    (every value a string), so flags, numbers, and IDs are parsed from either.
    It has no "item played" notification: completion arrives as PlaybackStop
    with PlayedToCompletion, or as UserDataSaved with Played and SaveReason.
    """
    if not isinstance(payload, dict):
        raise TypeError("Webhook payload must be an object")
    kind = _first(payload, "event_type", "NotificationType", "Event")
    mapping = {
        "PlaybackStart": "playback_started",
        "PlaybackProgress": "playback_progress",
        "PlaybackStop": "playback_stopped",
        "ItemPlayed": "item_played",
        "UserDataSaved": "user_data_saved",
    }
    kind = mapping.get(kind, kind)
    if kind not in {
        "playback_started",
        "playback_progress",
        "playback_stopped",
        "item_played",
        "favorite_changed",
        "user_data_saved",
    }:
        raise UnsupportedWebhookEvent(f"{kind or 'Unknown'} notifications are not used")
    item_id = _jellyfin_id(_first(payload, "item_external_id", "ItemId"))
    if not item_id:
        raise ValueError("Item ID is required")
    played = _flag(_first(payload, "played", "Played"))
    completed = _flag(_first(payload, "played_to_completion", "PlayedToCompletion"))
    reason = str(_first(payload, "save_reason", "SaveReason") or "").lower()
    if kind == "playback_stopped" and completed:
        kind = "item_played"
    elif kind == "user_data_saved":
        # Marking watched counts as completion; other saves carry favorite state.
        kind = (
            "item_played"
            if played and reason in PLAYED_SAVE_REASONS
            else "favorite_changed"
        )
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    event_id = _first(payload, "event_id", "EventId") or digest
    occurred = (
        _as_datetime(_first(payload, "occurred_at", "UtcTimestamp", "Date")) or utcnow()
    )
    return {
        "event_id": str(event_id),
        "source": "jellyfin",
        "event_type": kind,
        "occurred_at": occurred,
        "user_external_id": _jellyfin_id(_first(payload, "user_external_id", "UserId")),
        "item_external_id": item_id,
        "item_type": str(_first(payload, "item_type", "ItemType") or "").lower(),
        "series_external_id": _jellyfin_id(
            _first(payload, "series_external_id", "SeriesId")
        ),
        "season_number": _number(
            _first(payload, "season_number", "SeasonNumber", "ParentIndexNumber")
        ),
        "episode_number": _number(
            _first(payload, "episode_number", "EpisodeNumber", "IndexNumber")
        ),
        "played": bool(played or completed or kind == "item_played"),
        "position_ticks": _number(
            _first(payload, "position_ticks", "PlaybackPositionTicks")
        )
        or 0,
        "favorite": _flag(_first(payload, "favorite", "Favorite", "IsFavorite")),
        "payload_hash": digest,
    }


def _duplicate(existing):
    increment("duplicate_events_ignored")
    log_operation("event_duplicate", "ignored", event_id=existing.id)
    return existing, False


def ingest_event(payload):
    normalized = normalize_event(payload)
    existing = (
        db.session.query(LifecycleEvent)
        .filter_by(external_event_id=normalized["event_id"])
        .first()
    )
    if existing:
        return _duplicate(existing)
    event = LifecycleEvent(
        external_event_id=normalized["event_id"],
        source="jellyfin",
        event_type=normalized["event_type"],
        occurred_at=normalized["occurred_at"],
        jellyfin_user_id=normalized["user_external_id"],
        payload_hash=normalized["payload_hash"],
        normalized_json={
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in normalized.items()
        },
    )
    db.session.add(event)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = (
            db.session.query(LifecycleEvent)
            .filter_by(external_event_id=normalized["event_id"])
            .first()
        )
        if existing:
            return _duplicate(existing)
        raise
    return event, True


def _active_candidate(media_id):
    return (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.media_identity_id == media_id,
            PurgeCandidate.state.in_(ACTIVE_CANDIDATE_STATES),
        )
        .first()
    )


def _rescue(media, event):
    candidate = _active_candidate(media.id)
    if not candidate:
        return
    candidate.state = "RESCUED"
    candidate.scheduled_delete_at = None
    for action in (
        db.session.query(LifecycleAction)
        .filter_by(candidate_id=candidate.id)
        .filter(LifecycleAction.state.in_(["PENDING", "FAILED_RETRYABLE"]))
    ):
        action.state = "CANCELLED"
    audit(
        "candidate_rescued",
        media.id,
        "Playback resumed; pending cleanup cancelled.",
        f"rescue:{candidate.id}:{event.id}",
        candidate_id=candidate.id,
        event_id=event.id,
    )


def _part_for_event(media, event_data):
    item_id = event_data["item_external_id"]
    part = (
        db.session.query(MediaPart)
        .filter_by(media_identity_id=media.id, jellyfin_id=item_id)
        .first()
    )
    if not part and media.media_type == "series":
        part = (
            db.session.query(MediaPart)
            .filter_by(
                media_identity_id=media.id,
                kind="episode",
                season_number=event_data["season_number"],
                episode_number=event_data["episode_number"],
            )
            .first()
        )
    return part


def process_event(event):
    data = event.normalized_json
    media = (
        db.session.query(MediaIdentity)
        .filter_by(jellyfin_id=data["item_external_id"])
        .first()
    )
    if not media and data.get("series_external_id"):
        media = (
            db.session.query(MediaIdentity)
            .filter_by(jellyfin_id=str(data["series_external_id"]))
            .first()
        )
    if not media:
        part = (
            db.session.query(MediaPart)
            .filter_by(jellyfin_id=data["item_external_id"])
            .first()
        )
        media = db.session.get(MediaIdentity, part.media_identity_id) if part else None
    if not media:
        audit(
            "event_unmapped",
            None,
            "Jellyfin item has no known media identity.",
            f"unmapped:{event.id}",
            event_id=event.id,
        )
        event.processed_at = utcnow()
        db.session.commit()
        return
    event.media_identity_id = media.id
    user_id = data.get("user_external_id") or "unknown"
    state = (
        db.session.query(UserMediaState)
        .filter_by(media_identity_id=media.id, jellyfin_user_id=user_id)
        .first()
    )
    if not state:
        state = UserMediaState(media_identity_id=media.id, jellyfin_user_id=user_id)
        db.session.add(state)
    when = _as_datetime(data["occurred_at"]) or utcnow()
    if data["event_type"] != "favorite_changed":
        state.last_played_at = max(_as_datetime(state.last_played_at) or when, when)
        _rescue(media, event)
        audit(
            "playback_received",
            media.id,
            "Playback reset the inactivity timer.",
            f"playback:{event.id}",
            event_id=event.id,
        )
    if data.get("favorite") is not None:
        state.favorite = bool(data["favorite"])
    part = _part_for_event(media, data)
    if part and data.get("played") and data["event_type"] == "item_played":
        episode_state = (
            db.session.query(EpisodeUserState)
            .filter_by(media_part_id=part.id, jellyfin_user_id=user_id)
            .first()
        )
        if not episode_state:
            episode_state = EpisodeUserState(
                media_part_id=part.id, jellyfin_user_id=user_id
            )
            db.session.add(episode_state)
        if not episode_state.played:
            state.completed_episode_count += 1
        episode_state.played = True
        episode_state.last_played_at = when
        episode_state.playback_position_ticks = data.get("position_ticks", 0)
        if media.media_type == "series" and part.season_number is not None:
            _plan_acquisition(media, part.season_number, event)
    event.processed_at = utcnow()
    db.session.commit()


def user_display_name(user_id):
    """A Jellyfin user's name from the reconciliation cache, never a raw ID."""
    names = setting("jellyfin_user_names", {})
    return names.get(user_id) or "A viewer"


def episode_label(season, episode):
    return f"S{season:02d}E{episode:02d}"


def _episode_span(parts):
    labels = [
        episode_label(p.season_number, p.episode_number)
        for p in sorted(parts, key=lambda p: (p.season_number, p.episode_number))
    ]
    return labels[0] if len(labels) == 1 else f"{labels[0]}–{labels[-1]}"


def _request_season(
    media,
    season,
    reason,
    *,
    event=None,
    current_season=None,
    episode_filter=None,
    allow_search=True,
):
    """reason is the cause, e.g. "Kelden finished S01E01 of XYZ"; the helper
    adds what Curatarr is doing about it, which depends on air dates."""
    """Queue a Sonarr monitor/search action for a season's missing episodes.

    Returns the action, or None when nothing in the season is missing. The
    idempotency key covers the exact episode set and whether a search is
    requested, so the same request is never queued twice.
    """
    # Only episodes Sonarr tracks can be acquired. Jellyfin sometimes lists
    # extra entries (e.g. split double episodes) that Sonarr does not; their
    # has_file is unknown to Sonarr, and searching for them re-downloaded
    # whole seasons that were already present.
    missing = [
        p
        for p in media.parts
        if p.kind == "episode"
        and p.season_number == season
        and not p.has_file
        and p.sonarr_episode_id is not None
        and (episode_filter is None or episode_filter(p))
    ]
    if not missing:
        if episode_filter is None and _season_airing(media, season):
            return _monitor_airing_season(media, season, reason, event)
        return None
    # An episode without an air date is announced but not released; it is
    # monitored so Sonarr grabs it later, but searching now cannot find it.
    available = [
        p for p in missing if p.air_date and _as_datetime(p.air_date) <= utcnow()
    ]
    search_now = bool(available) and allow_search
    episode_ids = sorted(p.sonarr_episode_id for p in missing)
    if episode_filter is not None:
        what, it = _episode_span(missing), "them"
    elif season == current_season:
        what, it = f"the rest of Season {season}", "them"
    else:
        what, it = f"Season {season}", "it"
    if search_now:
        outcome = f"searching for {what}"
    elif available:
        outcome = f"monitoring {what}; the search follows in a later run"
    else:
        outcome = f"monitoring {what} so Sonarr downloads {it} when it airs"
    reason = f"{reason}, so Curatarr is {outcome}."
    signature = hashlib.sha256(
        json.dumps(
            {"episode_ids": episode_ids, "search_now": search_now}, sort_keys=True
        ).encode()
    ).hexdigest()[:12]
    return audit(
        "sonarr_season_search",
        media.id,
        reason,
        f"sonarr-season-search:{media.id}:{season}:{signature}",
        payload={
            "sonarr_id": media.sonarr_id,
            "season": season,
            "episode_ids": episode_ids,
            "search_now": search_now,
            "current_season": current_season,
            # A whole-season request also monitors the season itself, so episodes
            # announced later are monitored too. A partial fill (episode_filter,
            # e.g. the first episodes of Season 1) leaves the season alone.
            "whole_season": episode_filter is None,
        },
        state="PENDING",
        event_id=event.id if event else None,
    )


# A season counts as airing while it is the latest one Sonarr lists and one of
# its episodes aired within this many days. (Unaired episodes Sonarr knows about
# are requested as missing episodes, which monitors the season anyway.)
AIRING_RECENT_DAYS = 30


def _season_airing(media, season):
    from datetime import timedelta

    tracked = [
        p
        for p in media.parts
        if p.kind == "episode"
        and (p.season_number or 0) > 0
        and p.sonarr_episode_id is not None
    ]
    if not tracked or season != max(p.season_number for p in tracked):
        return False
    cutoff = utcnow() - timedelta(days=AIRING_RECENT_DAYS)
    return any(
        p.air_date and _as_datetime(p.air_date) >= cutoff
        for p in tracked
        if p.season_number == season
    )


def _monitor_airing_season(media, season, reason, event=None):
    """Make sure Sonarr monitors a season that is still airing even when every
    episode it lists is already downloaded, so new episodes download as they
    air. Queued once per season (an empty episode request with whole_season)."""
    reason = (
        f"{reason}, so Curatarr is making sure Sonarr monitors Season {season}, "
        "which is still airing, so new episodes download when they come out."
    )
    return audit(
        "sonarr_season_search",
        media.id,
        reason,
        f"sonarr-season-monitor:{media.id}:{season}",
        payload={
            "sonarr_id": media.sonarr_id,
            "season": season,
            "episode_ids": [],
            "search_now": False,
            "current_season": season,
            "whole_season": True,
        },
        state="PENDING",
        event_id=event.id if event else None,
    )


def _since_reset(media):
    """Filters limiting viewing to after a "Reset to minimum" (none if never reset)."""
    if media.viewing_reset_at is None:
        return []
    return [EpisodeUserState.last_played_at > media.viewing_reset_at]


def _plan_acquisition(media, current_season, event):
    policy, _ = resolved_policy(media)
    if current_season == 0 and not policy["manage_specials"]:
        return
    completed = (
        db.session.query(func.count(func.distinct(EpisodeUserState.media_part_id)))
        .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.media_identity_id == media.id,
            MediaPart.season_number == current_season,
            EpisodeUserState.played.is_(True),
            *_since_reset(media),
        )
        .scalar()
    )
    if completed < policy["acquisition_threshold"]:
        return
    seasons = acquisition_seasons(media.parts, current_season, policy)
    if not seasons or not media.sonarr_id:
        return
    acquisition = (
        db.session.query(AcquisitionState).filter_by(media_identity_id=media.id).first()
    )
    if not acquisition:
        acquisition = AcquisitionState(media_identity_id=media.id)
        db.session.add(acquisition)
    acquisition.state = "READY"
    acquisition.highest_demonstrated_season = max(
        acquisition.highest_demonstrated_season or 0, current_season
    )
    acquisition.target_next_season = seasons[-1]
    acquisition.plan_revision = (acquisition.plan_revision or 0) + 1
    acquisition.last_triggered_at = utcnow()
    current_missing = any(
        p.kind == "episode" and p.season_number == current_season and not p.has_file
        for p in media.parts
    )
    acquisition.state = "EXPANDING_CURRENT" if current_missing else "PREFETCHING_NEXT"
    planned = False
    pending = False
    failed = False
    for season in seasons:
        data = event.normalized_json or {}
        episode = data.get("episode_number")
        finished = (
            episode_label(current_season, episode)
            if episode is not None
            else f"an episode of Season {current_season}"
        )
        action = _request_season(
            media,
            season,
            f"{user_display_name(data.get('user_external_id'))} finished {finished} "
            f"of {media.title}",
            event=event,
            current_season=current_season,
        )
        if action is None:
            continue
        planned = True
        pending = pending or action.state in {"PENDING", "RUNNING", "FAILED_RETRYABLE"}
        failed = failed or action.state in {"FAILED_FINAL", "UNKNOWN_RECONCILE"}
    if failed:
        acquisition.state = "ERROR_RETRY"
    elif not planned or not pending:
        future = any(
            part.kind == "episode"
            and not part.has_file
            and part.season_number == acquisition.target_next_season
            and part.air_date
            and _as_datetime(part.air_date) > utcnow()
            for part in media.parts
        )
        acquisition.state = "WAITING_FOR_FUTURE" if future else "ACTIVE"


def process_pending_events(limit=100):
    from .leases import acquire, keep_alive, release

    events = (
        db.session.query(LifecycleEvent)
        .filter_by(processed_at=None)
        .order_by(LifecycleEvent.created_at)
        .limit(limit)
        .all()
    )
    processed = 0
    for event in events:
        scope = f"event:{event.id}"
        owner = acquire(scope, seconds=120)
        if not owner:
            continue
        try:
            db.session.refresh(event)
            if event.processed_at is None:
                started = time.monotonic()
                with keep_alive(scope, owner, seconds=120) as lost:
                    process_event(event)
                log_operation(
                    f"event_{event.event_type}",
                    "processed",
                    event_id=event.id,
                    media_identity_id=event.media_identity_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                if lost.is_set():
                    raise RuntimeError("Event processing lost its database lease")
                processed += 1
        except Exception:
            db.session.rollback()
            raise
        finally:
            release(scope, owner)
    return processed


def reconcile_user_state():
    """Repair missed playback and favorite changes from Jellyfin user data."""
    jellyfin = client("jellyfin")
    generated = 0
    users = jellyfin.users()
    set_setting(
        "jellyfin_user_names",
        setting("jellyfin_user_names", {})
        | {str(user["Id"]): str(user.get("Name") or user["Id"]) for user in users},
    )
    for user in users:
        user_id = str(user["Id"])
        for library in db.session.query(Library).filter_by(enabled=True).all():
            offset = 0
            while True:
                page = jellyfin.user_items(
                    user_id, library.jellyfin_library_id, offset, 100
                )
                rows = page.get("Items", [])
                for item in rows:
                    item_id = str(item.get("Id"))
                    kind = item.get("Type")
                    media = (
                        db.session.query(MediaIdentity)
                        .filter_by(jellyfin_id=item_id)
                        .first()
                    )
                    if not media and kind == "Episode":
                        media = (
                            db.session.query(MediaIdentity)
                            .filter_by(jellyfin_id=str(item.get("SeriesId")))
                            .first()
                        )
                    if not media:
                        continue
                    user_data = item.get("UserData") or {}
                    state = (
                        db.session.query(UserMediaState)
                        .filter_by(media_identity_id=media.id, jellyfin_user_id=user_id)
                        .first()
                    )
                    favorite = bool(user_data.get("IsFavorite", False))
                    if kind in {"Series", "Movie"} and favorite != (
                        state.favorite if state else False
                    ):
                        ingest_event(
                            {
                                "event_id": f"reconcile-favorite:{user_id}:{item_id}:{utcnow().isoformat()}",
                                "event_type": "favorite_changed",
                                "item_external_id": item_id,
                                "series_external_id": item.get("SeriesId"),
                                "user_external_id": user_id,
                                "favorite": favorite,
                            }
                        )
                        generated += 1
                    last = _as_datetime(user_data.get("LastPlayedDate"))
                    current = _as_datetime(state.last_played_at) if state else None
                    new_completion = False
                    if kind == "Episode" and user_data.get("Played"):
                        part = (
                            db.session.query(MediaPart)
                            .filter_by(media_identity_id=media.id, jellyfin_id=item_id)
                            .first()
                        )
                        if not part:
                            part = (
                                db.session.query(MediaPart)
                                .filter_by(
                                    media_identity_id=media.id,
                                    kind="episode",
                                    season_number=item.get("ParentIndexNumber"),
                                    episode_number=item.get("IndexNumber"),
                                )
                                .first()
                            )
                        episode_state = (
                            db.session.query(EpisodeUserState)
                            .filter_by(media_part_id=part.id, jellyfin_user_id=user_id)
                            .first()
                            if part
                            else None
                        )
                        new_completion = bool(
                            part and (not episode_state or not episode_state.played)
                        )
                    if new_completion or (last and (current is None or last > current)):
                        ingest_event(
                            {
                                "event_id": f"reconcile-playback:{user_id}:{item_id}:{(last or utcnow()).isoformat()}",
                                "event_type": "item_played"
                                if user_data.get("Played")
                                else "playback_stopped",
                                "item_external_id": item_id,
                                "series_external_id": item.get("SeriesId"),
                                "season_number": item.get("ParentIndexNumber"),
                                "episode_number": item.get("IndexNumber"),
                                "user_external_id": user_id,
                                "played": bool(user_data.get("Played")),
                                "position_ticks": user_data.get(
                                    "PlaybackPositionTicks", 0
                                ),
                                "occurred_at": (last or utcnow()).isoformat(),
                            }
                        )
                        generated += 1
                offset += len(rows)
                total = page.get("TotalRecordCount")
                if (
                    not rows
                    or (total is not None and offset >= total)
                    or (total is None and len(rows) < 100)
                ):
                    break
    process_pending_events(limit=max(100, generated))
    return generated


def queue_watch_restoration():
    """Schedule played-state restoration when previously removed episodes return."""
    queued = 0
    snapshots = db.session.query(WatchStateSnapshot).filter_by(restored_at=None).all()
    for snapshot in snapshots:
        media = db.session.get(MediaIdentity, snapshot.media_identity_id)
        if (not media or media.missing_since) and snapshot.provider_key.startswith(
            "tvdb:"
        ):
            try:
                tvdb_id = int(snapshot.provider_key.split(":", 1)[1])
            except ValueError:
                continue
            matches = (
                db.session.query(MediaIdentity)
                .filter_by(tvdb_id=tvdb_id, media_type="series", missing_since=None)
                .limit(2)
                .all()
            )
            media = matches[0] if len(matches) == 1 else None
        if not media:
            continue
        part = (
            db.session.query(MediaPart)
            .filter_by(
                media_identity_id=media.id,
                kind="episode",
                season_number=snapshot.season_number,
                episode_number=snapshot.episode_number,
            )
            .first()
        )
        if not part or not part.jellyfin_id or not part.has_file:
            continue
        if not snapshot.played:
            snapshot.restored_at = utcnow()
            continue
        action = audit(
            "jellyfin_restore_played",
            media.id,
            "Restore preserved played state after the episode returned.",
            f"restore-played:{snapshot.id}",
            state="PENDING",
            payload={
                "snapshot_id": snapshot.id,
                "user_id": snapshot.jellyfin_user_id,
                "item_id": part.jellyfin_id,
            },
        )
        if action.state == "PENDING":
            queued += 1
    db.session.commit()
    return queued


def reclaimed_bytes_total():
    return (
        db.session.query(func.sum(PurgeCandidate.reclaimable_bytes))
        .join(LifecycleAction, LifecycleAction.candidate_id == PurgeCandidate.id)
        .filter(
            LifecycleAction.action_type == "delete_media",
            LifecycleAction.state == "SUCCEEDED",
        )
        .scalar()
        or 0
    )


def metrics_summary():
    """Section 94 operational metrics from durable rows and counters."""
    from .observability import counter_values

    counters = counter_values()
    pending_states = ["PENDING", "FAILED_RETRYABLE", "RUNNING"]
    oldest = (
        db.session.query(func.min(LifecycleAction.created_at))
        .filter(LifecycleAction.state.in_(pending_states))
        .scalar()
    )
    count = db.session.query(func.count(PurgeCandidate.id))
    proposed = (
        db.session.query(func.sum(PurgeCandidate.reclaimable_bytes))
        .filter(PurgeCandidate.state.in_(ACTIVE_CANDIDATE_STATES))
        .scalar()
    )
    prefix = "external_api_failures."
    return {
        "events_processed": db.session.query(func.count(LifecycleEvent.id))
        .filter(LifecycleEvent.processed_at.isnot(None))
        .scalar(),
        "duplicate_events_ignored": counters["duplicate_events_ignored"],
        "acquisition_actions": db.session.query(func.count(LifecycleAction.id))
        .filter_by(action_type="sonarr_season_search")
        .scalar(),
        "purge_candidates_created": count.scalar(),
        "rescues": count.filter(PurgeCandidate.state == "RESCUED").scalar(),
        "bytes_proposed": proposed or 0,
        "bytes_reclaimed": reclaimed_bytes_total(),
        "external_api_failures": {
            kind: counters[prefix + kind] for kind in ("jellyfin", "sonarr", "radarr")
        },
        "last_reconcile_duration_ms": setting("last_reconcile_duration_ms"),
        "pending_actions": db.session.query(func.count(LifecycleAction.id))
        .filter(LifecycleAction.state.in_(pending_states))
        .scalar(),
        "oldest_pending_action_age_seconds": (
            round((utcnow() - _as_datetime(oldest)).total_seconds()) if oldest else None
        ),
    }


def save_policy_layer(library, form, keys):
    """Save submitted policy fields for one library, or globally when None.

    Every library's effective policy is validated against the new values
    first, so a global change cannot leave any library unresolvable.
    """
    from .models import LibraryPolicy
    from .policy import merge_policy_form

    global_values = setting("global_policy", {})
    if library is None:
        from .policy import DEFAULTS

        global_values = merge_policy_form(global_values, form, keys)
        # The global page shows every value; store only real changes so a
        # built-in default that changes later still applies.
        global_values = {
            key: value
            for key, value in global_values.items()
            if key not in DEFAULTS or value != DEFAULTS[key]
        }
        layers = {}
    else:
        row = library.policy or LibraryPolicy(library_id=library.id)
        layers = {library.id: merge_policy_form(row.policy_json, form, keys)}
    for media_type in ("series", "movie"):
        effective_policy(media_type, global_values, {})
    for other in db.session.query(Library).all():
        values = layers.get(other.id, other.policy.policy_json if other.policy else {})
        try:
            effective_policy(
                "series" if other.media_type == "tv" else "movie",
                global_values,
                values,
            )
        except ValueError as exc:
            raise ValueError(f"{other.name}: {exc}") from exc
    if library is None:
        set_setting("global_policy", global_values)
    else:
        row.policy_json = layers[library.id]
        db.session.add(row)
    db.session.commit()


def lifecycle_outlook(limit=5):
    """Upcoming scheduled deletions and the most recent cleanup outcomes."""
    scheduled = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state.in_(["LEAVING_SOON", "REVIEW"]),
            PurgeCandidate.scheduled_delete_at.isnot(None),
        )
        .order_by(PurgeCandidate.scheduled_delete_at)
        .limit(limit)
        .all()
    )
    cleanup = (
        db.session.query(LifecycleAction, PurgeCandidate)
        .join(PurgeCandidate, LifecycleAction.candidate_id == PurgeCandidate.id)
        .filter(
            (LifecycleAction.action_type == "would_delete")
            | (
                (LifecycleAction.action_type == "delete_media")
                & (LifecycleAction.state == "SUCCEEDED")
            )
        )
        .order_by(LifecycleAction.completed_at.desc())
        .limit(limit)
        .all()
    )
    return scheduled, cleanup


def setup_progress():
    """First-run checklist derived from stored state (spec section 40)."""
    integrations = {row.kind: row for row in db.session.query(Integration).all()}
    steps = []
    for kind in ("jellyfin", "sonarr", "radarr"):
        row = integrations.get(kind)
        steps.append(
            {
                "key": kind,
                "label": f"Connect {kind.title()} (URL, API key, connectivity test)",
                "done": bool(row and row.secret_ref and row.health_state == "healthy"),
                "endpoint": "main.settings",
            }
        )
    steps.append(
        {
            "key": "webhook",
            "label": "Create the Jellyfin webhook token",
            "done": setting("webhook_token") is not None,
            "endpoint": "main.settings",
        }
    )
    steps.append(
        {
            "key": "discover",
            "label": "Discover Jellyfin libraries",
            "done": db.session.query(Library.id).first() is not None,
            "endpoint": "main.overview",
        }
    )
    policies = db.session.query(LibraryPolicy.id).first() is not None or bool(
        setting("global_policy")
    )
    steps.append(
        {
            "key": "policy",
            "label": "Review acquisition and retention rules",
            "done": policies,
            "optional": True,
            "endpoint": "main.rules",
            "args": {"section": "acquisition"},
        }
    )
    return steps


def review_rows():
    """Pending candidates with the section 29 review context."""
    from .lifecycle import _candidate_input
    from .policy import retained

    names = setting("jellyfin_user_names", {})
    rows = []
    candidates = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state.in_(
                ["ELIGIBLE", "REVIEW", "LEAVING_SOON", "SNOOZED", "APPROVED"]
            )
        )
        .order_by(PurgeCandidate.eligible_at)
        .all()
    )
    for candidate in candidates:
        media = candidate.media
        policy, _ = resolved_policy(media)
        item = _candidate_input(media)
        latest = (
            db.session.query(UserMediaState)
            .filter(
                UserMediaState.media_identity_id == media.id,
                UserMediaState.last_played_at.isnot(None),
            )
            .order_by(UserMediaState.last_played_at.desc())
            .first()
        )
        if media.media_type == "series":
            files = {
                part.arr_file_id
                for part in media.parts
                if part.kind == "episode"
                and part.has_file
                and not retained(part, policy)
            }
            action = (
                f"Delete {len(files)} episode file{'s' if len(files) != 1 else ''} "
                "outside the retained footprint via Sonarr"
            )
            meaningful = item.completed_episodes >= policy["meaningful_threshold"]
        else:
            action = "Delete the movie and its files via Radarr"
            meaningful = item.last_played_at is not None
        rows.append(
            {
                "candidate": candidate,
                "media": media,
                "library": media.library.name,
                "last_played": item.last_played_at,
                "last_watcher": (
                    names.get(latest.jellyfin_user_id, latest.jellyfin_user_id)
                    if latest
                    else None
                ),
                "added_at": media.added_at,
                "favorite": item.favorite,
                "meaningful": meaningful,
                "proposed_action": action,
            }
        )
    return rows


def save_integration(kind, base_url, api_key):
    """Test and save one integration; raises ValueError with a user message."""
    from .integrations import normalized_url
    from .secrets import decrypt_secret, encrypt_secret

    url = normalized_url(base_url or "")
    row = db.session.query(Integration).filter_by(kind=kind).first()
    key = api_key
    if not key and row and row.secret_ref:
        try:
            key = decrypt_secret(row.secret_ref)
        except ValueError as exc:
            raise ValueError(
                "Saved key cannot be decrypted; provide a new API key"
            ) from exc
    if not key:
        raise ValueError("API key is required")
    try:
        version = CLIENTS[kind](url, key).version()
    except IntegrationError as exc:
        raise ValueError(
            f"{kind.title()} could not be reached; settings were not saved"
        ) from exc
    if not row:
        row = Integration(kind=kind, base_url=url)
        db.session.add(row)
    row.base_url = url
    row.secret_ref = encrypt_secret(key)
    row.detected_version = version
    row.health_state = "healthy"
    row.last_health_at = utcnow()
    db.session.commit()


def rotate_webhook_token(token=None):
    import secrets

    token = token or secrets.token_urlsafe(32)
    set_setting("webhook_token", token)
    db.session.commit()
    return token


def save_reconcile_interval(minutes):
    if type(minutes) is not int or not 5 <= minutes <= 1440:
        raise ValueError("Reconciliation interval must be 5–1440 minutes")
    set_setting("reconciliation_interval_seconds", minutes * 60)
    db.session.commit()


INTEGRATION_HELP = {
    "jellyfin": {
        "port": 8096,
        "key_path": "/web/#/dashboard/keys",
        "where": "Jellyfin Dashboard → API Keys → add a key (name it Curatarr)",
    },
    "sonarr": {
        "port": 8989,
        "key_path": "/settings/general",
        "where": "Sonarr Settings → General → Security → API Key",
    },
    "radarr": {
        "port": 7878,
        "key_path": "/settings/general",
        "where": "Radarr Settings → General → Security → API Key",
    },
}


def integration_guides(integrations):
    """Where to find each API key, with links built from known URLs.

    Sonarr and Radarr usually run on the Jellyfin host, so their suggested URL
    reuses its host with the app's default port.
    """
    from urllib.parse import urlparse

    jellyfin = integrations.get("jellyfin")
    host = urlparse(jellyfin.base_url).hostname if jellyfin else None
    guides = {}
    for kind, info in INTEGRATION_HELP.items():
        row = integrations.get(kind)
        suggested = f"http://{host}:{info['port']}" if host else None
        base = row.base_url if row else None
        guides[kind] = {
            "where": info["where"],
            "port": info["port"],
            "suggested_url": suggested,
            "key_url": (base or suggested) + info["key_path"]
            if (base or suggested)
            else None,
        }
    return guides


def settings_view():
    integrations = {row.kind: row for row in db.session.query(Integration)}
    return {
        "integrations": integrations,
        "guides": integration_guides(integrations),
        "webhook_template": JELLYFIN_WEBHOOK_TEMPLATE,
        "webhook_present": setting("webhook_token") is not None,
        "interval_minutes": setting("reconciliation_interval_seconds", 3600) // 60,
        "webhook": webhook_status(),
    }


def history_actions(filters, limit=200):
    """Filtered audit history; raises ValueError for malformed dates."""
    from datetime import date, timedelta

    query = db.session.query(LifecycleAction)
    if filters.get("type"):
        query = query.filter(LifecycleAction.action_type == filters["type"])
    if filters.get("state"):
        query = query.filter(LifecycleAction.state == filters["state"])
    if filters.get("q") or filters.get("library"):
        query = query.join(
            MediaIdentity, LifecycleAction.media_identity_id == MediaIdentity.id
        )
    if filters.get("q"):
        query = query.filter(MediaIdentity.title.ilike(f"%{filters['q']}%"))
    if filters.get("library"):
        query = query.filter(MediaIdentity.library_id == filters["library"])
    if filters.get("user"):
        query = query.join(
            LifecycleEvent, LifecycleAction.event_id == LifecycleEvent.id
        ).filter(LifecycleEvent.jellyfin_user_id == filters["user"])
    if filters.get("from"):
        start = datetime.combine(
            date.fromisoformat(filters["from"]), datetime.min.time(), UTC
        )
        query = query.filter(LifecycleAction.created_at >= start)
    if filters.get("to"):
        end = datetime.combine(
            date.fromisoformat(filters["to"]) + timedelta(days=1),
            datetime.min.time(),
            UTC,
        )
        query = query.filter(LifecycleAction.created_at < end)
    return query.order_by(LifecycleAction.created_at.desc()).limit(limit).all()


def media_titles_for(rows):
    ids = {row.media_identity_id for row in rows if row.media_identity_id}
    if not ids:
        return {}
    return {
        media.id: media.title
        for media in db.session.query(MediaIdentity).filter(MediaIdentity.id.in_(ids))
    }


def history_filter_options():
    names = setting("jellyfin_user_names", {})
    user_ids = {
        row[0]
        for row in db.session.query(LifecycleEvent.jellyfin_user_id).distinct()
        if row[0]
    } | set(names)
    return {
        "types": sorted(
            row[0] for row in db.session.query(LifecycleAction.action_type).distinct()
        ),
        "states": sorted(
            row[0] for row in db.session.query(LifecycleAction.state).distinct()
        ),
        "libraries": db.session.query(Library).order_by(Library.name).all(),
        "users": sorted(
            ((user_id, names.get(user_id, user_id)) for user_id in user_ids),
            key=lambda pair: pair[1].lower(),
        ),
    }


def action_context(action_id):
    """One audit row with its input event, media, and candidate timeline."""
    action = db.session.get(LifecycleAction, action_id)
    if not action:
        return None
    related = []
    decision = None
    if action.candidate_id:
        related = (
            db.session.query(LifecycleAction)
            .filter_by(candidate_id=action.candidate_id)
            .order_by(LifecycleAction.created_at)
            .all()
        )
        decision = next(
            (row for row in related if row.action_type == "candidate_created"), None
        )
    return {
        "action": action,
        "event": db.session.get(LifecycleEvent, action.event_id)
        if action.event_id
        else None,
        "media": db.session.get(MediaIdentity, action.media_identity_id)
        if action.media_identity_id
        else None,
        "decision": decision,
        "related": related,
    }


def integrations_ready():
    rows = {row.kind: row for row in db.session.query(Integration)}
    return all(
        rows.get(kind) is not None and rows[kind].secret_ref
        for kind in ("jellyfin", "sonarr", "radarr")
    )


def request_reconciliation():
    """Ask the worker to discover and reconcile on its next cycle.

    Discovery of a large library takes minutes, far longer than a web request
    may run, so the UI only records the request.
    """
    set_setting("reconcile_requested_at", utcnow().isoformat())


RECONCILE_STALE_MINUTES = 30


def reconciliation_state():
    """Queued, running, failed, interrupted, or idle, with times for display."""
    from datetime import timedelta

    now = utcnow()
    requested = _as_datetime(setting("reconcile_requested_at"))
    attempt = _as_datetime(setting("last_reconcile_attempt_at"))
    finished = _as_datetime(setting("last_reconcile_at"))
    error = setting("last_reconcile_error") or {}
    error_at = _as_datetime(error.get("at"))
    if requested and (not attempt or requested > attempt):
        state = "queued"
    elif attempt and (not finished or attempt > finished):
        if error_at and error_at >= attempt:
            state = "failed"
        elif now - attempt > timedelta(minutes=RECONCILE_STALE_MINUTES):
            state = "interrupted"
        else:
            state = "running"
    else:
        state = "idle"
    return {
        "state": state,
        "requested_at": requested,
        "started_at": attempt,
        "elapsed_seconds": round((now - attempt).total_seconds())
        if state == "running"
        else None,
        "last_finished": finished,
        "last_duration_ms": setting("last_reconcile_duration_ms"),
        "error": error.get("message") if state == "failed" else None,
        "titles": db.session.query(func.count(MediaIdentity.id))
        .filter(MediaIdentity.missing_since.is_(None))
        .scalar(),
        "libraries": db.session.query(func.count(Library.id))
        .filter(Library.media_type.in_(["tv", "movies"]))
        .scalar(),
    }


# Template for the Jellyfin Webhook plugin's Generic destination. Values are
# quoted so movies (no season/episode) still render valid JSON.
JELLYFIN_WEBHOOK_TEMPLATE = """{
  "event_type": "{{NotificationType}}",
  "occurred_at": "{{UtcTimestamp}}",
  "item_external_id": "{{ItemId}}",
  "item_type": "{{ItemType}}",
  "series_external_id": "{{SeriesId}}",
  "season_number": "{{SeasonNumber}}",
  "episode_number": "{{EpisodeNumber}}",
  "user_external_id": "{{UserId}}",
  "position_ticks": "{{PlaybackPositionTicks}}",
  "played_to_completion": "{{PlayedToCompletion}}",
  "played": "{{Played}}",
  "favorite": "{{Favorite}}",
  "save_reason": "{{SaveReason}}"
}"""


def dry_run_by_library():
    """Libraries split by their effective dry-run setting (library or global)."""
    global_values = setting("global_policy", {})
    split = {"dry_run": [], "deleting": []}
    for library in db.session.query(Library).order_by(Library.name):
        if not library.managed:
            continue
        values = library.policy.policy_json if library.policy else {}
        policy, _ = effective_policy(
            "series" if library.media_type == "tv" else "movie", global_values, values
        )
        split["dry_run" if policy["dry_run"] else "deleting"].append(library.name)
    return split


# New searches started per reconciliation; the rest follow in later runs, so a
# first sync of a large library cannot flood the indexers or the downloader.
ACQUISITION_SEARCHES_PER_RUN = 5


def _plan_show(media, request, now, *, require_recent=True):
    """Apply the always-keep fill and season-ahead catch-up to one show."""
    from datetime import timedelta

    from .policy import retained

    policy, _ = resolved_policy(media)
    if policy["fill_minimum_footprint"]:
        # "Entire series" means never trim, not download everything, so the
        # fill covers at most Season 1.
        request(
            media,
            1,
            f"{media.title} is missing episodes it always keeps",
            episode_filter=lambda part, policy=policy: retained(part, policy),
        )
    completed = (
        db.session.query(
            MediaPart.season_number,
            func.count(func.distinct(EpisodeUserState.media_part_id)),
            func.max(EpisodeUserState.last_played_at),
        )
        .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.media_identity_id == media.id,
            MediaPart.season_number > 0,
            EpisodeUserState.played.is_(True),
            *_since_reset(media),
        )
        .group_by(MediaPart.season_number)
        .all()
    )
    if not completed:
        return
    season, count, _last = max(completed, key=lambda row: row[0])
    last_played = max(
        (_as_datetime(row[2]) for row in completed if row[2]), default=None
    )
    recent = last_played and now - last_played <= timedelta(
        days=policy["tv_inactivity_days"]
    )
    if count < policy["acquisition_threshold"] or (
        require_recent and not recent and not _caught_up(media, season)
    ):
        return
    latest = (
        db.session.query(EpisodeUserState.jellyfin_user_id, MediaPart.episode_number)
        .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.media_identity_id == media.id,
            MediaPart.season_number == season,
            EpisodeUserState.played.is_(True),
            *_since_reset(media),
        )
        .order_by(EpisodeUserState.last_played_at.desc())
        .first()
    )
    # Past tense reads correctly for shared accounts like "Jon and Amber".
    cause = (
        f"{user_display_name(latest[0])} finished "
        f"{episode_label(season, latest[1])} of {media.title}, the latest "
        f"watched in Season {season}"
        if latest
        else f"Viewers are watching Season {season} of {media.title}"
    )
    for target in acquisition_seasons(media.parts, season, policy):
        request(media, target, cause, current_season=season)


def _caught_up(media, season):
    """Viewers watched the latest season available to them: `season` counts as
    watched (the caller checks acquisition_threshold), and the next season came
    out after the last time anyone played an episode of it. Such a show gets its
    next season whenever it appears, with no recent-viewing limit. A show not
    continued although the next season was already out is abandoned and keeps
    the limit."""
    watched_at = (
        db.session.query(func.max(EpisodeUserState.last_played_at))
        .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.media_identity_id == media.id,
            MediaPart.season_number == season,
            EpisodeUserState.played.is_(True),
            *_since_reset(media),
        )
        .scalar()
    )
    if watched_at is None:
        return False
    following = [
        p
        for p in media.parts
        if p.kind == "episode" and (p.season_number or 0) > season
    ]
    if not following:
        return False
    next_season = min(p.season_number for p in following)
    premieres = [
        _as_datetime(p.air_date)
        for p in following
        if p.season_number == next_season and p.air_date
    ]
    # An undated next season has not premiered yet, so it comes after.
    return not premieres or min(premieres) > _as_datetime(watched_at)


def _skip_reason(media):
    if media.media_type != "series" or media.sonarr_id is None:
        return "Only TV shows matched to Sonarr can be requested."
    if media.missing_since is not None:
        return "This show is no longer in Jellyfin."
    if media.arr_monitored is False:
        return "Sonarr has this show unmonitored, so Curatarr leaves it alone."
    if media.series_type == "daily":
        return "Daily shows are numbered by date, so Curatarr does not request them."
    return None


def reconcile_acquisition():
    """Request what viewing and the always-keep rule call for, every reconciliation.

    Covers what event-driven planning misses: always-keep episodes that were
    never downloaded, and a next season that appeared after viewers caught up.
    Shows Sonarr has unmonitored, and daily shows, are left alone.
    """
    now = utcnow()
    budget = ACQUISITION_SEARCHES_PER_RUN
    requested = 0

    def request(media, season, reason, **kwargs):
        nonlocal budget, requested
        action = _request_season(
            media, season, reason, allow_search=budget > 0, **kwargs
        )
        # Requests queued by an earlier run come back unchanged; only new ones
        # count, and only new searches use up this run's budget.
        if action is not None and _as_datetime(action.created_at) >= now:
            requested += 1
            if action.payload_json.get("search_now"):
                budget -= 1
        return action

    shows = (
        db.session.query(MediaIdentity)
        .filter(MediaIdentity.media_type == "series")
        .order_by(MediaIdentity.title)
        .all()
    )
    for media in shows:
        if _skip_reason(media) is None:
            _plan_show(media, request, now)
    db.session.commit()
    return requested


def request_title_now(media):
    """Run the acquisition rules for one show immediately, for the title page.

    Ignores the per-run search limit and the recent-viewing window. Returns
    (new actions, skip reason); the caller sends the actions to Sonarr.
    """
    reason = _skip_reason(media)
    if reason:
        return [], reason
    now = utcnow()
    created = []

    def request(media, season, cause, **kwargs):
        action = _request_season(media, season, cause, **kwargs)
        if action is not None and _as_datetime(action.created_at) >= now:
            created.append(action)
        return action

    _plan_show(media, request, now, require_recent=False)
    db.session.commit()
    return created, None


EVENT_LABELS = {
    "playback_started": "Playback Start",
    "playback_progress": "Playback Progress",
    "playback_stopped": "Playback Stop",
    "item_played": "Finished",
    "favorite_changed": "User Data Saved",
}


def record_webhook(outcome, *, reason=None, event=None):
    """Remember the latest webhook delivery or rejection for the Settings page."""
    entry = {"at": utcnow().isoformat(), "outcome": outcome}
    if reason:
        entry["reason"] = reason
    if event is not None:
        entry["event_id"] = event.id
    key = "webhook_last_delivery" if outcome == "accepted" else "webhook_last_problem"
    set_setting(key, entry)
    if outcome == "accepted":
        increment("webhook_deliveries")


def _media_for_delivery(data):
    """Best-effort title for a delivery that has not been processed yet."""
    ids = [
        i for i in (data.get("series_external_id"), data.get("item_external_id")) if i
    ]
    if not ids:
        return None
    media = (
        db.session.query(MediaIdentity)
        .filter(MediaIdentity.jellyfin_id.in_(ids))
        .first()
    )
    if media:
        return media
    part = (
        db.session.query(MediaPart)
        .filter(MediaPart.jellyfin_id == data.get("item_external_id"))
        .first()
    )
    return part.media if part else None


def webhook_status():
    """What the last webhook delivery was and whether it matched a title."""
    from .observability import counter_values

    delivery = setting("webhook_last_delivery")
    problem = setting("webhook_last_problem")
    summary = None
    if delivery:
        event = db.session.get(LifecycleEvent, delivery.get("event_id"))
        if event:
            data = event.normalized_json or {}
            media = (
                db.session.get(MediaIdentity, event.media_identity_id)
                if event.media_identity_id
                else _media_for_delivery(data)
            )
            what = (
                f"{episode_label(data['season_number'], data['episode_number'])} of "
                if data.get("season_number") is not None
                and data.get("episode_number") is not None
                else ""
            )
            summary = {
                "at": _as_datetime(delivery["at"]),
                "event": EVENT_LABELS.get(event.event_type, event.event_type),
                "description": f"{what}{media.title if media else 'an item'} by "
                f"{user_display_name(data.get('user_external_id'))}",
                "match": (
                    "waiting to be processed"
                    if event.processed_at is None
                    else f"matched to {media.title}"
                    if media
                    else "not matched: the item is not in a TV or movie library "
                    "Curatarr manages"
                ),
            }
    latest_problem = None
    if problem and (
        not delivery or _as_datetime(problem["at"]) > _as_datetime(delivery["at"])
    ):
        latest_problem = {
            "at": _as_datetime(problem["at"]),
            "outcome": problem["outcome"],
            "reason": problem.get("reason"),
        }
    return {
        "last": summary,
        "problem": latest_problem,
        "count": counter_values()["webhook_deliveries"],
    }


def app_version():
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("curatarr")
    except PackageNotFoundError:
        return "unknown"


def operational_status(*, demo_mode=False):
    """Non-secret operational state for /api/v1/status and Overview."""
    integrations = {kind: "unconfigured" for kind in CLIENTS}
    for row in db.session.query(Integration).all():
        integrations[row.kind] = row.health_state
    if demo_mode:
        outage = setting("demo_outage")
        if outage in integrations:
            integrations[outage] = "unhealthy (simulated)"
    split = dry_run_by_library()
    return {
        "app": "curatarr",
        "version": app_version(),
        "dry_run": not split["deleting"],
        "dry_run_libraries": split["dry_run"],
        "deleting_libraries": split["deleting"],
        "review_count": db.session.query(PurgeCandidate)
        .filter_by(state="REVIEW")
        .count(),
        "leaving_soon_count": db.session.query(PurgeCandidate)
        .filter_by(state="LEAVING_SOON")
        .count(),
        "reclaimed_bytes": reclaimed_bytes_total(),
        "pending_actions": db.session.query(LifecycleAction)
        .filter(LifecycleAction.state.in_(["PENDING", "FAILED_RETRYABLE", "RUNNING"]))
        .count(),
        "integrations": integrations,
    }


def overview_data():
    """Library sizes, recent decisions, errors, requests, and the cleanup outlook."""
    every_library = db.session.query(Library).order_by(Library.name).all()
    library_reports = []
    for library in every_library:
        if not library.managed:
            continue
        policy, _ = effective_policy(
            "series" if library.media_type == "tv" else "movie",
            setting("global_policy", {}),
            library.policy.policy_json if library.policy else {},
        )
        size = (
            db.session.query(func.sum(MediaPart.size_bytes))
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(
                MediaIdentity.library_id == library.id, MediaPart.has_file.is_(True)
            )
            .scalar()
            or 0
        )
        high = policy["high_water_bytes"] if policy["quota_enabled"] else None
        low = policy["low_water_bytes"] if policy["quota_enabled"] else None
        library_reports.append(
            {
                "library": library,
                "size": size,
                "high": high,
                "low": low,
                "pressure": "above high water"
                if high is not None and size > high
                else "normal",
                "free_space_enabled": policy["free_space_enabled"],
            }
        )
    recent = db.session.query(LifecycleAction).order_by(
        LifecycleAction.created_at.desc()
    )
    actions = recent.limit(10).all()
    errors = (
        recent.filter(
            LifecycleAction.state.in_(
                ["FAILED_RETRYABLE", "FAILED_FINAL", "UNKNOWN_RECONCILE"]
            )
        )
        .limit(5)
        .all()
    )
    acquisitions = (
        recent.filter(LifecycleAction.action_type == "sonarr_season_search")
        .limit(10)
        .all()
    )
    scheduled, cleanup = lifecycle_outlook()
    media_titles = media_titles_for(actions + acquisitions) | {
        row.media_identity_id: row.media.title
        for row in scheduled + [candidate for _action, candidate in cleanup]
    }
    return {
        "library_reports": library_reports,
        "unmanaged_libraries": [
            library.name for library in every_library if not library.managed
        ],
        "actions": actions,
        "errors": errors,
        "acquisitions": acquisitions,
        "scheduled": scheduled,
        "cleanup": cleanup,
        "media_titles": media_titles,
    }


TITLE_OVERRIDE_FIELDS = (
    "never_purge",
    "fill_minimum_footprint",
    "tv_inactivity_days",
    "movie_inactivity_days",
    "minimum_mode",
    "minimum_episodes",
    "acquisition_threshold",
    "meaningful_threshold",
    "grace_days",
    "review_mode",
    "purge_strategy",
)


def save_title_override(media, form):
    """Save one title's overrides; enabling Never Purge rescues it at once."""
    from .artwork import reconcile_artwork
    from .lifecycle import rescue_candidate
    from .models import TitleOverride
    from .policy import merge_policy_form

    override = media.override or TitleOverride(media_identity_id=media.id)
    override.override_json = merge_policy_form(
        override.override_json or {}, form, TITLE_OVERRIDE_FIELDS
    )
    db.session.add(override)
    db.session.commit()
    if override.override_json.get("never_purge"):
        candidate = _active_candidate(media.id)
        if candidate:
            rescue_candidate(candidate, "Never Purge was enabled for this title.")
            reconcile_artwork()


def title_view(media):
    from .policy import reclaimable_bytes, retained

    values, sources = resolved_policy(media)
    episodes = [p for p in media.parts if p.kind == "episode"]
    return {
        "values": values,
        "sources": sources,
        # For "Reset to minimum": what would be removed and what stays.
        "reset_bytes": reclaimable_bytes(media.parts, values),
        "reset_files": sum(
            1 for p in episodes if p.has_file and not retained(p, values)
        ),
        "reset_kept": sum(1 for p in episodes if p.has_file and retained(p, values)),
        "candidates": db.session.query(PurgeCandidate)
        .filter_by(media_identity_id=media.id)
        .order_by(PurgeCandidate.eligible_at.desc())
        .limit(5)
        .all(),
        "actions": db.session.query(LifecycleAction)
        .filter_by(media_identity_id=media.id)
        .order_by(LifecycleAction.created_at.desc())
        .limit(10)
        .all(),
    }
