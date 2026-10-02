"""Reconciliation, review actions, and guarded external execution."""

from datetime import timedelta

from sqlalchemy import func, update

from . import db
from .integrations import IntegrationError
from .models import (
    AcquisitionState,
    EpisodeUserState,
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
    CandidateInput,
    _aware,
    eligible,
    inactivity_due,
    minimum_satisfied,
    rank,
    reclaimable_bytes,
    select_to_low_water,
    weighted_score,
)
from .services import _active_candidate, audit, client, resolved_policy


def _candidate_input(media, active_queue=False):
    states = (
        db.session.query(UserMediaState).filter_by(media_identity_id=media.id).all()
    )
    last = max((s.last_played_at for s in states if s.last_played_at), default=None)
    favorite = any(s.favorite for s in states)
    policy, _ = resolved_policy(media)
    if media.media_type == "series":
        size = reclaimable_bytes(media.parts, policy)
        completed = (
            db.session.query(func.count(func.distinct(EpisodeUserState.media_part_id)))
            .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
            .filter(
                MediaPart.media_identity_id == media.id,
                EpisodeUserState.played.is_(True),
            )
            .scalar()
            or 0
        )
        mapped = media.sonarr_id is not None
        if not minimum_satisfied(media.parts, policy):
            mapped = False
    else:
        size = sum(p.size_bytes for p in media.parts if p.has_file)
        completed = int(last is not None)
        mapped = media.radarr_id is not None
    existing = _active_candidate(media.id)
    added_at = media.added_at or media.created_at
    latest_acquisition = max(
        (_aware(part.acquired_at) for part in media.parts if part.acquired_at),
        default=None,
    )
    if latest_acquisition:
        added_at = max(_aware(added_at), _aware(latest_acquisition))
    return CandidateInput(
        media.id,
        media.media_type,
        added_at,
        last,
        size,
        completed,
        favorite,
        policy["never_purge"],
        active_queue,
        existing.snooze_until if existing else None,
        mapped,
    )


def _queue_ids():
    """Return externally active arr IDs; failure excludes all affected media."""
    result = {}
    for kind in ("sonarr", "radarr"):
        try:
            payload = client(kind).queue()
            rows = payload.get("records", []) if isinstance(payload, dict) else payload
            key = "seriesId" if kind == "sonarr" else "movieId"
            result[kind] = {row.get(key) for row in rows if row.get(key) is not None}
        except IntegrationError:
            result[kind] = None
    return result


def _disk_pressure(media, policy):
    if not policy["free_space_enabled"]:
        return None
    kind = "sonarr" if media.media_type == "series" else "radarr"
    try:
        disks = client(kind).diskspace()
    except IntegrationError:
        return None
    configured = policy["disk_path"].rstrip("/") or "/"
    matching = []
    for disk in disks:
        root = (disk.get("path") or "").rstrip("/") or "/"
        if configured == root or configured.startswith(root.rstrip("/") + "/"):
            matching.append((len(root), disk))
    if not matching:
        return None
    disk = max(matching, key=lambda pair: pair[0])[1]
    total = disk.get("totalSpace") or 0
    free = disk.get("freeSpace") or 0
    if total <= 0 or free < 0 or free > total:
        return None
    percent = free / total * 100
    if percent >= policy["low_free_percent"]:
        return (0, "normal")
    level = "critical" if percent < policy["critical_free_percent"] else "low"
    required = max(0, int(total * policy["low_free_percent"] / 100 - free))
    return required, level


def _selected_capacity_ids(media, policy, reason_code, active_ids, now):
    """Re-rank a capacity decision using the latest persisted observations."""
    library_media = (
        db.session.query(MediaIdentity)
        .filter_by(library_id=media.library_id, missing_since=None)
        .all()
    )
    inputs = []
    current_size = 0
    for other in library_media:
        current_size += sum(part.size_bytes for part in other.parts if part.has_file)
        arr_id = other.sonarr_id if other.media_type == "series" else other.radarr_id
        candidate = _candidate_input(other, arr_id in active_ids)
        other_policy, _ = resolved_policy(other)
        if eligible(candidate, other_policy, now):
            inputs.append(candidate)
    ranked = rank(inputs, policy["purge_strategy"], now, policy["meaningful_threshold"])
    if reason_code == "quota":
        if not policy["quota_enabled"] or current_size <= policy["high_water_bytes"]:
            return set()
        required = current_size - policy["low_water_bytes"]
    else:
        pressure = _disk_pressure(media, policy)
        if not pressure:
            return set()
        required = pressure[0]
    selected = set()
    reclaimed = 0
    for item in ranked:
        if reclaimed >= required:
            break
        selected.add(item.media_id)
        reclaimed += item.size_bytes
    return selected


def rescue_candidate(candidate, reason):
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
        candidate.media_identity_id,
        reason,
        f"policy-rescue:{candidate.id}",
        candidate_id=candidate.id,
    )
    db.session.commit()


def reconcile_candidates():
    """Remove stale candidates before evaluating fresh retention state."""
    now = utcnow()
    active = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state.in_(
                ["ELIGIBLE", "REVIEW", "LEAVING_SOON", "SNOOZED", "APPROVED"]
            )
        )
        .all()
    )
    rescued = 0
    for candidate in active:
        media = db.session.get(MediaIdentity, candidate.media_identity_id)
        reason = None
        if not media or media.missing_since:
            reason = "Media is no longer present."
        else:
            policy, _ = resolved_policy(media)
            item = _candidate_input(media)
            if policy["never_purge"]:
                reason = "Never Purge is now enabled."
            elif not item.mapped or item.size_bytes <= 0:
                reason = "Media mapping or reclaimable content changed."
            elif item.last_played_at and (
                candidate.last_activity_snapshot is None
                or _aware(item.last_played_at)
                > _aware(candidate.last_activity_snapshot)
            ):
                reason = "Playback occurred after candidate creation."
            elif candidate.reason_code == "inactivity" and not inactivity_due(
                item, policy, now
            ):
                reason = "Inactivity rule no longer applies."
            elif candidate.reason_code == "quota":
                current_size = (
                    db.session.query(func.sum(MediaPart.size_bytes))
                    .join(
                        MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id
                    )
                    .filter(
                        MediaIdentity.library_id == media.library_id,
                        MediaPart.has_file.is_(True),
                    )
                    .scalar()
                    or 0
                )
                if (
                    not policy["quota_enabled"]
                    or current_size <= policy["high_water_bytes"]
                ):
                    reason = "Library is no longer above high-water mark."
            elif candidate.reason_code == "disk_pressure":
                pressure = _disk_pressure(media, policy)
                if pressure is not None and pressure[0] <= 0:
                    reason = "Disk pressure has cleared."
        if reason:
            rescue_candidate(candidate, reason)
            rescued += 1
    return rescued


def evaluate_retention():
    """Create safe review/automatic candidates from observed media state."""
    from .leases import acquire, release

    scope = "library_policy_eval:all"
    owner = acquire(scope, seconds=3600)
    if not owner:
        return 0
    try:
        return _evaluate_retention_locked()
    except Exception:
        db.session.rollback()
        raise
    finally:
        release(scope, owner)


def _evaluate_retention_locked():
    reconcile_candidates()
    queue = _queue_ids()
    now = utcnow()
    created = 0
    for library in db.session.query(MediaIdentity.library_id).distinct():
        media_rows = (
            db.session.query(MediaIdentity).filter_by(library_id=library[0]).all()
        )
        if not media_rows:
            continue
        candidates = []
        current_size = 0
        for media in media_rows:
            if media.missing_since:
                continue
            kind = "sonarr" if media.media_type == "series" else "radarr"
            active_ids = queue[kind]
            arr_id = media.sonarr_id if kind == "sonarr" else media.radarr_id
            current_size += sum(p.size_bytes for p in media.parts if p.has_file)
            item = _candidate_input(
                media, active_ids is not None and arr_id in active_ids
            )
            policy, _ = resolved_policy(media)
            if eligible(item, policy, now, healthy=active_ids is not None):
                candidates.append((media, item, policy))
        if not candidates:
            continue
        library_policy = candidates[0][2]
        selected_quota = set()
        ranked_items = rank(
            [row[1] for row in candidates],
            library_policy["purge_strategy"],
            now,
            library_policy["meaningful_threshold"],
        )
        if library_policy["quota_enabled"]:
            selected, _deficit = select_to_low_water(
                ranked_items,
                current_size,
                library_policy["high_water_bytes"],
                library_policy["low_water_bytes"],
            )
            selected_quota = {item.media_id for item in selected}
            if _deficit:
                audit(
                    "capacity_deficit",
                    None,
                    f"Library cannot reach low-water target; {_deficit} bytes remain after all eligible candidates.",
                    f"quota-deficit:{media_rows[0].library_id}:{current_size}:{_deficit}",
                )
        selected_pressure = set()
        pressure = _disk_pressure(media_rows[0], library_policy)
        if pressure and pressure[0] > 0:
            reclaimed = 0
            for ranked in ranked_items:
                selected_pressure.add(ranked.media_id)
                reclaimed += ranked.size_bytes
                if reclaimed >= pressure[0]:
                    break
            if reclaimed < pressure[0]:
                deficit = pressure[0] - reclaimed
                audit(
                    "capacity_deficit",
                    None,
                    f"Disk pressure remains; {deficit} bytes cannot be reclaimed from eligible media.",
                    f"disk-deficit:{media_rows[0].library_id}:{pressure[0]}:{deficit}",
                )
        for media, item, policy in candidates:
            if _active_candidate(media.id):
                continue
            due = inactivity_due(item, policy, now)
            quota = media.id in selected_quota
            under_pressure = media.id in selected_pressure
            if not due and not quota and not under_pressure:
                continue
            reason_code = (
                "inactivity" if due else ("quota" if quota else "disk_pressure")
            )
            reason = (
                f"No playback within {policy['tv_inactivity_days'] if media.media_type == 'series' else policy['movie_inactivity_days']} days."
                if due
                else (
                    f"Library exceeds {policy['high_water_bytes']} bytes; selection targets {policy['low_water_bytes']} bytes."
                    if quota
                    else f"Disk pressure is {pressure[1]}; selection targets {policy['low_free_percent']}% free space."
                )
            )
            score, detail = weighted_score(item, now, policy["meaningful_threshold"])
            review_mode = policy["review_mode"]
            if reason_code == "disk_pressure":
                _, sources = resolved_policy(media)
                if sources["review_mode"] != "title":
                    review_mode = (
                        policy[f"{pressure[1]}_pressure_review_mode"] or review_mode
                    )
                detail["pressure_level"] = pressure[1]
            state = (
                "ELIGIBLE"
                if review_mode == "recommend"
                else ("REVIEW" if review_mode == "require_review" else "LEAVING_SOON")
            )
            candidate = PurgeCandidate(
                media_identity_id=media.id,
                state=state,
                reason_code=reason_code,
                reason_text=reason,
                reclaimable_bytes=item.size_bytes,
                score=score,
                score_detail_json=detail,
                last_activity_snapshot=item.last_played_at,
                scheduled_delete_at=(
                    now + timedelta(days=policy["notice_days"])
                    if state == "LEAVING_SOON"
                    or (
                        state == "REVIEW"
                        and policy["review_expiry"] == "auto_delete_after_notice"
                    )
                    else None
                ),
            )
            db.session.add(candidate)
            db.session.flush()
            audit(
                "candidate_created",
                media.id,
                reason,
                f"candidate:{candidate.id}",
                candidate_id=candidate.id,
                payload={
                    "reclaimable_bytes": item.size_bytes,
                    "score": score,
                    "score_detail": detail,
                    "effective_policy": policy,
                },
            )
            created += 1
    db.session.commit()
    return created


def review_candidate(candidate_id, choice):
    candidate = db.session.get(PurgeCandidate, candidate_id)
    if not candidate or candidate.state not in {
        "REVIEW",
        "ELIGIBLE",
        "LEAVING_SOON",
        "SNOOZED",
    }:
        raise ValueError("Candidate is not available for review")
    media = db.session.get(MediaIdentity, candidate.media_identity_id)
    if choice == "delete" and candidate.state == "ELIGIBLE":
        raise ValueError("Recommend-only mode cannot approve deletion")
    now = utcnow()
    if choice == "keep":
        candidate.state = "RESCUED"
        candidate.scheduled_delete_at = None
    elif choice == "never_purge":
        from .models import TitleOverride

        override = media.override or TitleOverride(media_identity_id=media.id)
        override.override_json = (override.override_json or {}) | {"never_purge": True}
        db.session.add(override)
        candidate.state = "RESCUED"
        candidate.scheduled_delete_at = None
    elif choice in {"snooze_30", "snooze_90"}:
        candidate.state = "SNOOZED"
        candidate.snooze_until = now + timedelta(days=int(choice.split("_")[1]))
        candidate.scheduled_delete_at = None
    elif choice == "delete":
        candidate.state = "APPROVED"
        candidate.approved_at = now
    else:
        raise ValueError("Unknown review action")
    audit(
        "review_" + choice,
        media.id,
        f"Review decision: {choice.replace('_', ' ')}.",
        f"review:{candidate.id}:{choice}:{candidate.candidate_revision}",
        candidate_id=candidate.id,
    )
    db.session.commit()
    return candidate


def _validate_deletion(candidate, *, queue=None):
    """Check every destructive precondition with fresh external observations."""
    media = db.session.get(MediaIdentity, candidate.media_identity_id)
    if not media or media.missing_since or candidate.state != "APPROVED":
        return "Item or approval is no longer valid"
    if db.session.query(LifecycleEvent).filter_by(processed_at=None).first():
        return "Unprocessed playback events must be reconciled first"
    policy, _ = resolved_policy(media)
    if policy["never_purge"]:
        return "Never Purge is enabled"
    if media.media_type == "series" and not minimum_satisfied(media.parts, policy):
        return "Minimum TV footprint is not fully present"
    now = utcnow()
    item = _candidate_input(media)
    if not eligible(item, policy, now):
        return "Grace, mapping, snooze, or minimum-footprint guard failed"
    last = item.last_played_at
    snapshot = candidate.last_activity_snapshot
    if last and (snapshot is None or last > snapshot):
        return "Playback occurred after candidate creation"
    if candidate.reason_code == "inactivity" and not inactivity_due(item, policy, now):
        return "Inactivity rule no longer applies"
    if candidate.reason_code == "quota":
        size = (
            db.session.query(func.sum(MediaPart.size_bytes))
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(
                MediaIdentity.library_id == media.library_id,
                MediaPart.has_file.is_(True),
            )
            .scalar()
            or 0
        )
        if not policy["quota_enabled"] or size <= policy["high_water_bytes"]:
            return "Library is no longer above high-water mark"
    if candidate.reason_code == "disk_pressure":
        pressure = _disk_pressure(media, policy)
        if not pressure or pressure[0] <= 0:
            return "Disk pressure is no longer confirmed"
    kind = "sonarr" if media.media_type == "series" else "radarr"
    try:
        arr = client(kind)
        rows = arr.queue()
        records = rows.get("records", []) if isinstance(rows, dict) else rows
        arr_id = media.sonarr_id if kind == "sonarr" else media.radarr_id
        field = "seriesId" if kind == "sonarr" else "movieId"
        if any(row.get(field) == arr_id for row in records):
            return "Download or import is active"
        if candidate.reason_code in {"quota", "disk_pressure"}:
            active_ids = {row.get(field) for row in records}
            selected = _selected_capacity_ids(
                media, policy, candidate.reason_code, active_ids, now
            )
            if media.id not in selected:
                return "Capacity ranking no longer selects this title"
        external = arr.request(
            "GET", f"/api/v3/{'series' if kind == 'sonarr' else 'movie'}/{arr_id}"
        )
        expected = media.tvdb_id if kind == "sonarr" else media.tmdb_id
        actual = external.get("tvdbId") if kind == "sonarr" else external.get("tmdbId")
        if expected is None or str(actual) != str(expected):
            return "External provider identity does not match"
        jellyfin = client("jellyfin")
        jellyfin_item = jellyfin.item(media.jellyfin_id)
        provider_key = "Tvdb" if kind == "sonarr" else "Tmdb"
        providers = jellyfin_item.get("ProviderIds") or {}
        if str(providers.get(provider_key)) != str(expected):
            return "Jellyfin provider identity does not match"
        if kind == "sonarr":
            _tv_file_targets(media, policy, arr)
    except (TypeError, ValueError) as exc:
        return str(exc)
    except IntegrationError:
        return "External integration cannot be verified"
    return None


def _tv_file_targets(media, policy, arr):
    """Require current Sonarr mappings to agree before deleting any episode file."""
    from .policy import retained

    local = {
        part.sonarr_episode_id: part
        for part in media.parts
        if part.kind == "episode" and part.sonarr_episode_id is not None
    }
    targets = {
        part.arr_file_id
        for part in media.parts
        if part.kind == "episode" and part.has_file and not retained(part, policy)
    }
    if not targets or None in targets:
        raise ValueError("Target episode files are not fully mapped")
    remote_episodes = arr.episodes(media.sonarr_id)
    remote_files = arr.episode_files(media.sonarr_id)
    if not isinstance(remote_episodes, list) or not isinstance(remote_files, list):
        raise TypeError("Sonarr episode-file inventory is incomplete")
    if any(not isinstance(row, dict) for row in remote_files + remote_episodes):
        raise TypeError("Sonarr episode-file inventory is incomplete")
    file_ids = {row.get("id") for row in remote_files}
    if not targets <= file_ids:
        raise ValueError("Sonarr episode-file inventory changed")
    seen = set()
    for episode in remote_episodes:
        episode_id = episode.get("id")
        file_id = episode.get("episodeFileId") or None
        if episode.get("hasFile") and file_id is None:
            raise ValueError("Sonarr episode-file inventory is incomplete")
        part = local.get(episode_id)
        if part:
            seen.add(episode_id)
            if part.has_file != bool(episode.get("hasFile")) or (
                part.has_file and part.arr_file_id != file_id
            ):
                raise ValueError("Sonarr episode-file mapping changed")
        if file_id in targets and (not part or retained(part, policy)):
            raise ValueError("Sonarr file overlaps retained or unknown episodes")
    if any(
        part.has_file and episode_id not in seen for episode_id, part in local.items()
    ):
        raise ValueError("Sonarr episode inventory is incomplete")
    return targets


def execute_approved(candidate_id):
    candidate = db.session.get(PurgeCandidate, candidate_id)
    if not candidate:
        raise ValueError("Unknown candidate")
    reason = _validate_deletion(candidate)
    if reason:
        candidate.state = "BLOCKED"
        audit(
            "delete_blocked",
            candidate.media_identity_id,
            reason,
            f"blocked:{candidate.id}:{candidate.candidate_revision}",
            candidate_id=candidate.id,
        )
        db.session.commit()
        return "blocked"
    media = db.session.get(MediaIdentity, candidate.media_identity_id)
    policy, _ = resolved_policy(media)
    if policy["dry_run"]:
        candidate.state = "COMPLETED"
        candidate.completed_at = utcnow()
        audit(
            "would_delete",
            media.id,
            f"Dry run: would reclaim {candidate.reclaimable_bytes} bytes. {candidate.reason_text}",
            f"dry-run:{candidate.id}:{candidate.candidate_revision}",
            candidate_id=candidate.id,
        )
        db.session.commit()
        return "dry_run"
    action = audit(
        "delete_media",
        media.id,
        candidate.reason_text,
        f"delete:{candidate.id}:{candidate.candidate_revision}",
        state="PENDING",
        candidate_id=candidate.id,
    )
    candidate.state = "EXECUTING"
    db.session.commit()
    return execute_action(action.id)


def _snapshot_watch_state(media):
    from .models import EpisodeUserState

    for part in media.parts:
        if part.kind != "episode":
            continue
        for state in db.session.query(EpisodeUserState).filter_by(
            media_part_id=part.id
        ):
            db.session.add(
                WatchStateSnapshot(
                    media_identity_id=media.id,
                    jellyfin_user_id=state.jellyfin_user_id,
                    provider_key=f"tvdb:{media.tvdb_id}",
                    season_number=part.season_number,
                    episode_number=part.episode_number,
                    played=state.played,
                    playback_position_ticks=state.playback_position_ticks,
                    date_played=state.last_played_at,
                )
            )
    db.session.commit()


def execute_action(action_id):
    action = db.session.get(LifecycleAction, action_id)
    if not action or action.state not in {"PENDING", "FAILED_RETRYABLE"}:
        return "skipped"
    claimed = db.session.execute(
        update(LifecycleAction)
        .where(
            LifecycleAction.id == action_id,
            LifecycleAction.state.in_(["PENDING", "FAILED_RETRYABLE"]),
        )
        .values(state="RUNNING", started_at=utcnow())
    ).rowcount
    db.session.commit()
    if claimed != 1:
        return "skipped"
    action = db.session.get(LifecycleAction, action_id)
    media = db.session.get(MediaIdentity, action.media_identity_id)
    if action.action_type == "sonarr_season_search":
        try:
            arr = client("sonarr")
            for episode_id in action.payload_json.get("episode_ids", []):
                arr.monitor_episode(episode_id)
            if action.payload_json.get("search_now"):
                queue = arr.queue()
                records = queue.get("records", []) if isinstance(queue, dict) else queue
                series_id = action.payload_json["sonarr_id"]
                season = action.payload_json["season"]
                episode_ids = set(action.payload_json.get("episode_ids", []))
                already_queued = any(
                    row.get("seriesId") == series_id
                    and (
                        row.get("seasonNumber") == season
                        or row.get("episodeId") in episode_ids
                    )
                    for row in records
                )
                try:
                    commands = arr.commands()
                except IntegrationError as exc:
                    if exc.status_code != 404:
                        raise
                    commands = []
                if isinstance(commands, dict):
                    commands = commands.get("records", [])
                from .services import _as_datetime

                action_created = _as_datetime(action.created_at)
                command_exists = any(
                    command.get("name") == "SeasonSearch"
                    and (command.get("body") or command).get("seriesId") == series_id
                    and (command.get("body") or command).get("seasonNumber") == season
                    and command.get("status") != "failed"
                    and (
                        queued_at := _as_datetime(
                            command.get("queued") or command.get("started")
                        )
                    )
                    is not None
                    and queued_at >= action_created
                    for command in commands
                )
                if not already_queued and not command_exists:
                    arr.season_search(series_id, season)
        except IntegrationError:
            action.state = "FAILED_RETRYABLE"
            action.attempts += 1
            action.next_retry_at = utcnow() + timedelta(
                minutes=min(60, 2**action.attempts)
            )
            action.last_error = "Sonarr acquisition request failed"
            acquisition = (
                db.session.query(AcquisitionState)
                .filter_by(media_identity_id=media.id)
                .first()
            )
            if acquisition:
                acquisition.state = "ERROR_RETRY"
                acquisition.last_error = action.last_error
            db.session.commit()
            return "failed"
        acquisition = (
            db.session.query(AcquisitionState)
            .filter_by(media_identity_id=media.id)
            .first()
        )
        if acquisition:
            pending = (
                db.session.query(LifecycleAction)
                .filter(
                    LifecycleAction.media_identity_id == media.id,
                    LifecycleAction.action_type == "sonarr_season_search",
                    LifecycleAction.id != action.id,
                    LifecycleAction.state.in_(
                        ["PENDING", "FAILED_RETRYABLE", "RUNNING"]
                    ),
                )
                .all()
            )
            if pending:
                current = action.payload_json.get("current_season")
                acquisition.state = (
                    "EXPANDING_CURRENT"
                    if any(row.payload_json.get("season") == current for row in pending)
                    else "PREFETCHING_NEXT"
                )
            else:
                from .services import _as_datetime

                future = any(
                    part.kind == "episode"
                    and not part.has_file
                    and part.season_number == acquisition.target_next_season
                    and part.air_date
                    and _as_datetime(part.air_date) > utcnow()
                    for part in media.parts
                )
                acquisition.state = "WAITING_FOR_FUTURE" if future else "ACTIVE"
            acquisition.last_error = None
    elif action.action_type == "jellyfin_restore_played":
        try:
            client("jellyfin").mark_played(
                action.payload_json["user_id"], action.payload_json["item_id"]
            )
            snapshot = db.session.get(
                WatchStateSnapshot, action.payload_json["snapshot_id"]
            )
            snapshot.restored_at = utcnow()
        except IntegrationError:
            action.state = "FAILED_RETRYABLE"
            action.attempts += 1
            action.next_retry_at = utcnow() + timedelta(
                minutes=min(60, 2**action.attempts)
            )
            action.last_error = "Jellyfin played-state restoration failed"
            db.session.commit()
            return "failed"
    elif action.action_type == "delete_media":
        candidate = db.session.get(PurgeCandidate, action.candidate_id)
        if not candidate or candidate.state != "EXECUTING":
            action.state = "CANCELLED"
            action.last_error = "Candidate was rescued or changed before execution"
            db.session.commit()
            return "blocked"
        candidate.state = "APPROVED"
        reason = _validate_deletion(candidate)
        candidate.state = "EXECUTING"
        if reason:
            candidate.state = "BLOCKED"
            action.state = "CANCELLED"
            action.last_error = reason
            db.session.commit()
            return "blocked"
        try:
            if media.media_type == "movie":
                client("radarr").delete_movie(media.radarr_id)
            else:
                arr = client("sonarr")
                policy, _ = resolved_policy(media)
                target_files = _tv_file_targets(media, policy, arr)
                _snapshot_watch_state(media)
                from .policy import retained

                for file_id in sorted(target_files):
                    arr.delete_episode_file(file_id)
                for part in media.parts:
                    if (
                        part.kind == "episode"
                        and part.has_file
                        and not retained(part, policy)
                    ):
                        part.has_file = False
        except (TypeError, ValueError) as exc:
            candidate.state = "BLOCKED"
            action.state = "CANCELLED"
            action.last_error = str(exc)
            db.session.commit()
            return "blocked"
        except IntegrationError:
            action.state = "UNKNOWN_RECONCILE"
            action.last_error = "Deletion response uncertain; reconcile before retry"
            db.session.commit()
            return "unknown"
        candidate.state = "COMPLETED"
        candidate.completed_at = utcnow()
    action.state = "SUCCEEDED"
    action.completed_at = utcnow()
    action.attempts += 1
    db.session.commit()
    return "succeeded"


def run_actions(limit=50):
    now = utcnow()
    rows = (
        db.session.query(LifecycleAction)
        .filter(
            LifecycleAction.state.in_(["PENDING", "FAILED_RETRYABLE"]),
            (
                LifecycleAction.next_retry_at.is_(None)
                | (LifecycleAction.next_retry_at <= now)
            ),
        )
        .order_by(LifecycleAction.created_at)
        .limit(limit)
        .all()
    )
    for action in rows:
        execute_action(action.id)
    return len(rows)


def reconcile_unknown_actions():
    """Resolve uncertain deletion responses without repeating the delete call."""
    actions = (
        db.session.query(LifecycleAction)
        .filter_by(action_type="delete_media", state="UNKNOWN_RECONCILE")
        .all()
    )
    resolved = 0
    for action in actions:
        media = db.session.get(MediaIdentity, action.media_identity_id)
        candidate = db.session.get(PurgeCandidate, action.candidate_id)
        if not media or not candidate:
            action.state = "FAILED_FINAL"
            action.last_error = (
                "Identity missing during uncertain-response reconciliation"
            )
            db.session.commit()
            resolved += 1
            continue
        try:
            if media.media_type == "movie":
                try:
                    client("radarr").movie(media.radarr_id)
                    complete = False
                except IntegrationError as exc:
                    if exc.status_code != 404:
                        continue
                    complete = True
            else:
                from .policy import retained

                policy, _ = resolved_policy(media)
                files = client("sonarr").episode_files(media.sonarr_id)
                present_ids = {row.get("id") for row in files}
                targets = [
                    part
                    for part in media.parts
                    if part.kind == "episode"
                    and part.has_file
                    and not retained(part, policy)
                ]
                if not targets or any(part.arr_file_id is None for part in targets):
                    complete = False
                else:
                    complete = all(
                        part.arr_file_id not in present_ids for part in targets
                    )
                    for part in targets:
                        if part.arr_file_id not in present_ids:
                            part.has_file = False
        except IntegrationError:
            continue
        if complete:
            candidate.state = "COMPLETED"
            candidate.completed_at = utcnow()
            action.state = "SUCCEEDED"
            action.completed_at = utcnow()
            reason = "External state confirms deletion completed after an uncertain response."
        else:
            candidate.state = "BLOCKED"
            action.state = "FAILED_FINAL"
            reason = "External state could not confirm complete deletion; manual review required."
            action.last_error = reason
        audit(
            "delete_reconciled",
            media.id,
            reason,
            f"delete-reconcile:{action.id}",
            candidate_id=candidate.id,
        )
        db.session.commit()
        resolved += 1
    return resolved


def recover_stale_actions(minutes=10):
    """Move interrupted work into a state that cannot blindly repeat writes."""
    from .policy import _aware

    now = utcnow()
    rows = db.session.query(LifecycleAction).filter_by(state="RUNNING").all()
    recovered = 0
    for action in rows:
        if not action.started_at or _aware(action.started_at) > now - timedelta(
            minutes=minutes
        ):
            continue
        action.state = (
            "UNKNOWN_RECONCILE"
            if action.action_type == "delete_media"
            else "FAILED_FINAL"
        )
        action.last_error = (
            "Worker stopped during external action; inspect before retry"
        )
        recovered += 1
    db.session.commit()
    return recovered


def expire_notices():
    now = utcnow()
    rows = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state.in_(["LEAVING_SOON", "REVIEW"]),
            PurgeCandidate.scheduled_delete_at <= now,
        )
        .all()
    )
    for candidate in rows:
        candidate.state = "APPROVED"
        candidate.approved_at = now
        db.session.commit()
        execute_approved(candidate.id)
    return len(rows)


def expire_snoozes():
    rows = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state == "SNOOZED",
            PurgeCandidate.snooze_until <= utcnow(),
        )
        .all()
    )
    for candidate in rows:
        candidate.state = "RESCUED"
        audit(
            "snooze_expired",
            candidate.media_identity_id,
            "Snooze expired; policy will be evaluated again.",
            f"snooze-expired:{candidate.id}",
            candidate_id=candidate.id,
        )
    db.session.commit()
    return len(rows)
