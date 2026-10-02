"""Application services for discovery, events, and lifecycle decisions."""

import hashlib
import json
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
    LifecycleAction,
    LifecycleEvent,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    UserMediaState,
    WatchStateSnapshot,
    utcnow,
)
from .policy import (
    acquisition_seasons,
    effective_policy,
)


def setting(key, default=None):
    row = db.session.query(AppSetting).filter_by(key=key).first()
    return row.value_json if row else default


def set_setting(key, value):
    row = db.session.query(AppSetting).filter_by(key=key).first()
    if not row:
        row = AppSetting(key=key, value_json=value)
        db.session.add(row)
    else:
        row.value_json = value
    db.session.commit()


def client(kind):
    if current_app.config["DEMO_MODE"]:
        from .demo import demo_client

        return demo_client(kind)
    row = db.session.query(Integration).filter_by(kind=kind, enabled=True).first()
    if not row or not row.secret_ref:
        raise IntegrationError(f"{kind} is not configured")
    return CLIENTS[kind](row.base_url, row.secret_ref)


def check_integration(kind):
    row = db.session.query(Integration).filter_by(kind=kind).first()
    if not row:
        return "unconfigured"
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
    if has_file and not part.has_file:
        part.acquired_at = utcnow()
    part.has_file = has_file
    part.size_bytes = item.get("Size") or part.size_bytes
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
                        file_sizes = {
                            entry["id"]: entry.get("size", 0)
                            for entry in sonarr.episode_files(media.sonarr_id)
                        }
                        for episode in sonarr.episodes(media.sonarr_id):
                            part = _upsert_part(
                                media,
                                "episode",
                                episode.get("seasonNumber"),
                                episode.get("episodeNumber"),
                                {"HasFile": episode.get("hasFile", False)},
                            )
                            part.sonarr_episode_id = episode.get("id")
                            part.arr_file_id = episode.get("episodeFileId") or None
                            part.size_bytes = file_sizes.get(part.arr_file_id, 0)
                            part.air_date = _as_datetime(episode.get("airDateUtc"))
                if media_type == "movie" and media.tmdb_id:
                    match = movies_by_tmdb.get(str(media.tmdb_id))
                    if match:
                        media.radarr_id = match["id"]
                        file_data = match.get("movieFile") or {}
                        _upsert_part(
                            media,
                            "movie_file",
                            None,
                            None,
                            {
                                "HasFile": match.get("hasFile", False),
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


def normalize_event(payload):
    """Accept Jellyfin notification plugin's common fields and canonical form."""
    if not isinstance(payload, dict):
        raise TypeError("Webhook payload must be an object")
    kind = (
        payload.get("event_type")
        or payload.get("NotificationType")
        or payload.get("Event")
    )
    mapping = {
        "PlaybackStart": "playback_started",
        "PlaybackProgress": "playback_progress",
        "PlaybackStop": "playback_stopped",
        "ItemPlayed": "item_played",
        "UserDataSaved": "favorite_changed",
    }
    kind = mapping.get(kind, kind)
    if kind not in {
        "playback_started",
        "playback_progress",
        "playback_stopped",
        "item_played",
        "favorite_changed",
    }:
        raise ValueError("Unsupported webhook event")
    item_id = payload.get("item_external_id") or payload.get("ItemId")
    if not item_id:
        raise ValueError("Item ID is required")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    event_id = payload.get("event_id") or payload.get("EventId") or digest
    occurred = (
        _as_datetime(payload.get("occurred_at") or payload.get("Date")) or utcnow()
    )
    return {
        "event_id": str(event_id),
        "source": "jellyfin",
        "event_type": kind,
        "occurred_at": occurred,
        "user_external_id": payload.get("user_external_id") or payload.get("UserId"),
        "item_external_id": str(item_id),
        "item_type": (
            payload.get("item_type") or payload.get("ItemType") or ""
        ).lower(),
        "series_external_id": payload.get("series_external_id")
        or payload.get("SeriesId"),
        "season_number": payload.get("season_number", payload.get("ParentIndexNumber")),
        "episode_number": payload.get("episode_number", payload.get("IndexNumber")),
        "played": bool(
            payload.get("played", payload.get("Played", kind == "item_played"))
        ),
        "position_ticks": int(
            payload.get("position_ticks", payload.get("PlaybackPositionTicks", 0)) or 0
        ),
        "favorite": payload.get("favorite", payload.get("IsFavorite")),
        "payload_hash": digest,
    }


def ingest_event(payload):
    normalized = normalize_event(payload)
    existing = (
        db.session.query(LifecycleEvent)
        .filter_by(external_event_id=normalized["event_id"])
        .first()
    )
    if existing:
        return existing, False
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
            return existing, False
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
        parts = [
            p for p in media.parts if p.kind == "episode" and p.season_number == season
        ]
        missing = [p for p in parts if not p.has_file]
        if not missing:
            continue
        planned = True
        available = [
            p
            for p in missing
            if p.air_date is None or _as_datetime(p.air_date) <= utcnow()
        ]
        reason = f"Season {season} requested because a Season {current_season} episode was completed."
        episode_ids = sorted(
            p.sonarr_episode_id for p in missing if p.sonarr_episode_id
        )
        signature = hashlib.sha256(
            json.dumps(
                {"episode_ids": episode_ids, "search_now": bool(available)},
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        action = audit(
            "sonarr_season_search",
            media.id,
            reason,
            f"sonarr-season-search:{media.id}:{season}:{signature}",
            payload={
                "sonarr_id": media.sonarr_id,
                "season": season,
                "episode_ids": episode_ids,
                "search_now": bool(available),
                "current_season": current_season,
            },
            state="PENDING",
            event_id=event.id,
        )
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
    from .leases import acquire, release

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
                process_event(event)
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
    for user in jellyfin.users():
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
                            .filter_by(jellyfin_id=item_id)
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
                if not rows or offset >= page.get("TotalRecordCount", offset):
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
