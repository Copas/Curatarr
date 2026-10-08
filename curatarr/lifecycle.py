"""Reconciliation, review actions, and guarded external execution."""

import logging
import time
from datetime import timedelta
from math import isfinite

from sqlalchemy import func, update

from . import db
from .integrations import IntegrationError
from .models import (
    AcquisitionState,
    EpisodeUserState,
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
from .observability import log_operation
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
            rows = _queue_records(client(kind).queue())
            key = "seriesId" if kind == "sonarr" else "movieId"
            result[kind] = {row.get(key) for row in rows if row.get(key) is not None}
        except IntegrationError:
            result[kind] = None
    return result


def _queue_records(payload):
    rows = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise IntegrationError("Integration queue response is invalid")
    return rows


def _local_disk_space(path):
    """Free space measured directly when Curatarr shares the arr apps' host.

    Used only when the arr apps report no disk for the path; returns None (no
    selection) when the path is not visible here, as in a separate container.
    """
    import os

    if not os.path.isdir(path):
        return None
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    return stats.f_blocks * stats.f_frsize, stats.f_bavail * stats.f_frsize


def _disk_measure(media, policy):
    """(total, free, disk key) for a library's configured path, or None."""
    import os

    if not policy["free_space_enabled"]:
        return None
    kind = "sonarr" if media.media_type == "series" else "radarr"
    try:
        disks = client(kind).diskspace()
    except IntegrationError:
        return None
    if not isinstance(disks, list) or any(not isinstance(disk, dict) for disk in disks):
        return None
    configured = policy["disk_path"].rstrip("/") or "/"
    matching = []
    for disk in disks:
        root = (disk.get("path") or "").rstrip("/") or "/"
        # "/" contains every path, so it only counts when it is the configured
        # path itself. Sonarr/Radarr omit network mounts (e.g. a CIFS NAS) from
        # disk space, and falling back to "/" measured the wrong disk.
        if root == "/" and configured != "/":
            continue
        if configured == root or configured.startswith(root + "/"):
            matching.append((len(root), root, disk))
    if matching:
        _length, root, disk = max(matching, key=lambda entry: entry[0])
        total = disk.get("totalSpace") or 0
        free = disk.get("freeSpace") or 0
        key = ("arr", root, total)
    else:
        measured = _local_disk_space(configured)
        if measured is None:
            return None
        total, free = measured
        key = ("local", os.stat(configured).st_dev)
    if (
        type(total) not in (int, float)
        or type(free) not in (int, float)
        or not isfinite(total)
        or not isfinite(free)
        or total <= 0
        or free < 0
        or free > total
    ):
        return None
    return total, free, key


def _disk_pressure(media, policy):
    """(bytes needed to reach the low threshold, level) or None if unknown."""
    measured = _disk_measure(media, policy)
    if measured is None:
        return None
    total, free, _key = measured
    percent = free / total * 100
    if percent >= policy["low_free_percent"]:
        return (0, "normal")
    level = "critical" if percent < policy["critical_free_percent"] else "low"
    required = max(0, int(total * policy["low_free_percent"] / 100 - free))
    return required, level


def _pressure_groups(now, queue, *, free_percent=None, disk_path=None):
    """Libraries grouped by the disk they live on, with each disk's shortfall.

    free_percent and disk_path are only for the cleanup preview: they pretend
    free space is at that level and enforcement is on with that path. Returns
    (groups by disk key, names of libraries whose disk could not be measured).
    """
    preview = free_percent is not None
    groups, unmeasured = {}, []
    libraries = (
        db.session.query(Library)
        .filter(Library.media_type.in_(["tv", "movies"]))
        .order_by(Library.name)
        .all()
    )
    for library in libraries:
        rows = (
            db.session.query(MediaIdentity)
            .filter_by(library_id=library.id, missing_since=None)
            .all()
        )
        if not rows:
            continue
        policy, _ = resolved_policy(rows[0])
        if preview:
            policy = policy | {
                "free_space_enabled": True,
                "disk_path": disk_path or policy["disk_path"],
            }
            if not policy["disk_path"]:
                unmeasured.append(library.name)
                continue
        measured = _disk_measure(rows[0], policy)
        if measured is None:
            if preview:
                unmeasured.append(library.name)
            continue
        total, actual_free, key = measured
        free = total * free_percent / 100 if preview else actual_free
        percent = free / total * 100
        if percent >= policy["low_free_percent"]:
            continue
        needed = max(0, int(total * policy["low_free_percent"] / 100 - free))
        level = "critical" if percent < policy["critical_free_percent"] else "low"
        group = groups.setdefault(
            key,
            {
                "needed": 0,
                "level": "low",
                "items": [],
                "policies": [],
                "libraries": [],
                "total": total,
                "actual_free": actual_free,
                "free": free,
            },
        )
        group["needed"] = max(group["needed"], needed)
        if level == "critical":
            group["level"] = "critical"
        group["policies"].append(policy)
        group["libraries"].append(library.name)
        for media in rows:
            kind = "sonarr" if media.media_type == "series" else "radarr"
            active_ids = queue.get(kind)
            arr_id = media.sonarr_id if kind == "sonarr" else media.radarr_id
            item = _candidate_input(
                media, active_ids is not None and arr_id in active_ids
            )
            media_policy, _ = resolved_policy(media)
            if eligible(item, media_policy, now, healthy=active_ids is not None):
                group["items"].append(item)
    return groups, unmeasured


def _select_from_group(group, now):
    """Titles picked from one disk's pool, in order, until its shortfall is met."""
    if len(group["policies"]) == 1:
        policy = group["policies"][0]
        strategy, meaningful = policy["purge_strategy"], policy["meaningful_threshold"]
    else:
        # Mixed TV and movie libraries: longest since last activity first,
        # favorites last, which every per-type strategy agrees with.
        strategy, meaningful = "oldest_watched_nonfavorite_first", 2
    picks, reclaimed = [], 0
    for item in rank(group["items"], strategy, now, meaningful):
        if reclaimed >= group["needed"]:
            break
        picks.append(item)
        reclaimed += item.size_bytes
    return picks, max(0, group["needed"] - reclaimed)


def _pressure_selection(now, queue):
    """Disk-pressure picks shared by every library on the same disk.

    Libraries are grouped by the disk they actually live on, the shortfall is
    computed once per disk, and titles are chosen from the combined pool until
    it is covered, so several libraries on one disk never each select enough
    for the whole shortfall. Returns ({media_id: level}, [(disk, deficit)]).
    """
    groups, _unmeasured = _pressure_groups(now, queue)
    selected, deficits = {}, []
    for key, group in groups.items():
        picks, deficit = _select_from_group(group, now)
        for item in picks:
            selected[item.media_id] = group["level"]
        if deficit:
            deficits.append((key, deficit))
    return selected, deficits


def preview_cleanup(free_percent, disk_path=None):
    """What free-space cleanup would pick if free space were at free_percent.

    Uses the same grouping, eligibility, ranking, and stopping rule as the
    worker against live queue state, but marks and changes nothing.
    """
    now = utcnow()
    groups, unmeasured = _pressure_groups(
        now, _queue_ids(), free_percent=free_percent, disk_path=disk_path
    )
    disks = []
    for group in groups.values():
        picks, deficit = _select_from_group(group, now)
        titles = {
            media.id: media
            for media in db.session.query(MediaIdentity).filter(
                MediaIdentity.id.in_([item.media_id for item in picks])
            )
        }
        running = 0
        rows = []
        for item in picks:
            running += item.size_bytes
            media = titles[item.media_id]
            rows.append(
                {
                    "media": media,
                    "library": media.library.name,
                    "size": item.size_bytes,
                    "running": running,
                    "last_activity": item.last_played_at,
                    "added": item.added_at,
                    "favorite": item.favorite,
                }
            )
        disks.append(
            {
                "libraries": group["libraries"],
                "total": group["total"],
                "actual_free": group["actual_free"],
                "free": group["free"],
                "needed": group["needed"],
                "level": group["level"],
                "picks": rows,
                "deficit": deficit,
                "eligible": len(group["items"]),
            }
        )
    return {"disks": disks, "unmeasured": unmeasured}


def _library_stored_bytes(library_id):
    return (
        db.session.query(func.sum(MediaPart.size_bytes))
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            MediaIdentity.library_id == library_id,
            MediaIdentity.missing_since.is_(None),
            MediaPart.has_file.is_(True),
        )
        .scalar()
        or 0
    )


def _quota_cleanup_active(media, policy, candidate):
    """Apply high/low-water hysteresis to a queued quota candidate.

    Above high water the run is active. Between the marks it continues only
    when Curatarr itself has deleted quota media since this candidate was
    created; a drop caused elsewhere still invalidates queued candidates.
    """
    if not policy["quota_enabled"]:
        return False
    size = _library_stored_bytes(media.library_id)
    if size > policy["high_water_bytes"]:
        return True
    if size <= policy["low_water_bytes"]:
        return False
    return (
        db.session.query(LifecycleAction.id)
        .join(PurgeCandidate, LifecycleAction.candidate_id == PurgeCandidate.id)
        .join(MediaIdentity, PurgeCandidate.media_identity_id == MediaIdentity.id)
        .filter(
            MediaIdentity.library_id == media.library_id,
            PurgeCandidate.reason_code == "quota",
            LifecycleAction.action_type == "delete_media",
            LifecycleAction.state == "SUCCEEDED",
            LifecycleAction.completed_at >= candidate.eligible_at,
        )
        .first()
        is not None
    )


def _selected_capacity_ids(media, policy, candidate, active_ids, now):
    """Re-rank a capacity decision using the latest persisted observations."""
    library_media = (
        db.session.query(MediaIdentity)
        .filter_by(library_id=media.library_id, missing_since=None)
        .all()
    )
    inputs = []
    for other in library_media:
        arr_id = other.sonarr_id if other.media_type == "series" else other.radarr_id
        item = _candidate_input(other, arr_id in active_ids)
        other_policy, _ = resolved_policy(other)
        if eligible(item, other_policy, now):
            inputs.append(item)
    ranked = rank(inputs, policy["purge_strategy"], now, policy["meaningful_threshold"])
    if candidate.reason_code == "quota":
        if not _quota_cleanup_active(media, policy, candidate):
            return set()
        required = _library_stored_bytes(media.library_id) - policy["low_water_bytes"]
    else:
        # Must match how evaluation chose it: shared across libraries on a disk.
        return set(_pressure_selection(now, _queue_ids())[0])
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
            elif (
                candidate.reason_code == "inactivity"
                and not policy["inactivity_cleanup"]
            ):
                reason = "Inactivity cleanup is turned off for this library."
            elif candidate.reason_code == "inactivity" and not inactivity_due(
                item, policy, now
            ):
                reason = "Inactivity rule no longer applies."
            elif candidate.reason_code == "quota":
                if not _quota_cleanup_active(media, policy, candidate):
                    reason = "Library no longer requires quota cleanup."
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
    from .leases import acquire, keep_alive, release

    scope = "library_policy_eval:all"
    owner = acquire(scope, seconds=3600)
    if not owner:
        return 0
    try:
        with keep_alive(scope, owner, seconds=3600) as lost:
            result = _evaluate_retention_locked()
        if lost.is_set():
            raise RuntimeError("Policy evaluation lost its database lease")
        return result
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
    pressure_selected, pressure_deficits = _pressure_selection(now, queue)
    for disk, deficit in pressure_deficits:
        audit(
            "capacity_deficit",
            None,
            f"Disk pressure remains; {deficit} bytes cannot be reclaimed from eligible media.",
            f"disk-deficit:{disk}:{deficit}",
        )
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
        for media, item, policy in candidates:
            if _active_candidate(media.id):
                continue
            # Unwatched time alone triggers cleanup only when a library opts in;
            # otherwise it only decides who goes first under space pressure.
            due = policy["inactivity_cleanup"] and inactivity_due(item, policy, now)
            quota = media.id in selected_quota
            pressure_level = pressure_selected.get(media.id)
            under_pressure = pressure_level is not None
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
                    else f"Disk pressure is {pressure_level}; selection targets {policy['low_free_percent']}% free space."
                )
            )
            score, detail = weighted_score(item, now, policy["meaningful_threshold"])
            review_mode = policy["review_mode"]
            rule_mode_key = (
                f"{pressure_level}_pressure_review_mode"
                if reason_code == "disk_pressure"
                else f"{reason_code}_review_mode"
            )
            # A title-level review mode is the most specific choice and wins.
            _, sources = resolved_policy(media)
            if sources["review_mode"] != "title":
                review_mode = policy[rule_mode_key] or review_mode
            if reason_code == "disk_pressure":
                detail["pressure_level"] = pressure_level
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
                    # Critical free space removes without a notice period.
                    now
                    if state == "LEAVING_SOON"
                    and reason_code == "disk_pressure"
                    and pressure_level == "critical"
                    else now + timedelta(days=policy["notice_days"])
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


PENDING_EVENTS_REASON = "Unprocessed playback events must be reconciled first"


def _validate_deletion(candidate, *, states=("APPROVED",)):
    """Check every destructive precondition with fresh external observations."""
    media = db.session.get(MediaIdentity, candidate.media_identity_id)
    if not media or media.missing_since or candidate.state not in states:
        return "Item or approval is no longer valid"
    if db.session.query(LifecycleEvent).filter_by(processed_at=None).first():
        return PENDING_EVENTS_REASON
    policy, _ = resolved_policy(media)
    if policy["never_purge"]:
        return "Never Purge is enabled"
    if (
        media.media_type == "series"
        and candidate.reason_code != MANUAL_RESET
        and not minimum_satisfied(media.parts, policy)
    ):
        # A requested reset may run with always-keep episodes missing: it only
        # removes episodes outside the minimum, and watching later fills it in.
        return "Minimum TV footprint is not fully present"
    now = utcnow()
    item = _candidate_input(media)
    if candidate.reason_code == MANUAL_RESET:
        # Asked for on the title page: the grace period, recent viewing, and
        # Sonarr's series monitored flag do not apply; every other check does.
        if media.sonarr_id is None:
            return "Show is not matched to Sonarr"
    elif not eligible(item, policy, now):
        return "Grace, mapping, snooze, or minimum-footprint guard failed"
    last = item.last_played_at
    snapshot = candidate.last_activity_snapshot
    if last and (snapshot is None or last > snapshot):
        return "Playback occurred after candidate creation"
    if candidate.reason_code == "inactivity" and not policy["inactivity_cleanup"]:
        return "Inactivity cleanup is turned off"
    if candidate.reason_code == "inactivity" and not inactivity_due(item, policy, now):
        return "Inactivity rule no longer applies"
    if candidate.reason_code == "quota" and not _quota_cleanup_active(
        media, policy, candidate
    ):
        return "Library is no longer above high-water mark"
    if candidate.reason_code == "disk_pressure":
        pressure = _disk_pressure(media, policy)
        if not pressure or pressure[0] <= 0:
            return "Disk pressure is no longer confirmed"
    kind = "sonarr" if media.media_type == "series" else "radarr"
    try:
        arr = client(kind)
        records = _queue_records(arr.queue())
        arr_id = media.sonarr_id if kind == "sonarr" else media.radarr_id
        field = "seriesId" if kind == "sonarr" else "movieId"
        if any(row.get(field) == arr_id for row in records):
            return "Download or import is active"
        if candidate.reason_code in {"quota", "disk_pressure"}:
            active_ids = {row.get(field) for row in records}
            selected = _selected_capacity_ids(media, policy, candidate, active_ids, now)
            if media.id not in selected:
                return "Capacity ranking no longer selects this title"
        external = arr.request(
            "GET", f"/api/v3/{'series' if kind == 'sonarr' else 'movie'}/{arr_id}"
        )
        if not isinstance(external, dict):
            return "External integration cannot be verified"
        expected = media.tvdb_id if kind == "sonarr" else media.tmdb_id
        actual = external.get("tvdbId") if kind == "sonarr" else external.get("tmdbId")
        if expected is None or str(actual) != str(expected):
            return "External provider identity does not match"
        if kind == "radarr":
            remote_size = external.get("sizeOnDisk")
            if remote_size is None and isinstance(external.get("movieFile"), dict):
                remote_size = external["movieFile"].get("size")
            local_size = sum(part.size_bytes for part in media.parts if part.has_file)
            if remote_size is not None and (
                type(remote_size) is not int or remote_size != local_size
            ):
                return "Radarr file size changed since discovery"
        jellyfin = client("jellyfin")
        jellyfin_item = jellyfin.item(media.jellyfin_id)
        if not isinstance(jellyfin_item, dict):
            return "External integration cannot be verified"
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
    local_file_sizes = {}
    for part in media.parts:
        if part.kind == "episode" and part.has_file and part.arr_file_id is not None:
            local_file_sizes[part.arr_file_id] = max(
                local_file_sizes.get(part.arr_file_id, 0), part.size_bytes
            )
    for row in remote_files:
        remote_size = row.get("size")
        if (
            row.get("id") in local_file_sizes
            and remote_size is not None
            and (
                type(remote_size) is not int
                or remote_size != local_file_sizes[row["id"]]
            )
        ):
            raise ValueError("Sonarr file size changed since discovery")
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


MANUAL_RESET = "manual_reset"


def reset_series(media):
    """Title page "Reset to minimum": trim a show back to its always-keep
    episodes as if nobody had watched it.

    Goes through the same approved-deletion path as the Review queue's Delete,
    with every safety check except the grace period and recent viewing. After the
    files are deleted, all seasons and the trimmed episodes are unmonitored in
    Sonarr and viewing before the reset stops driving acquisition, so Curatarr
    fetches more only once someone watches again. Dry run is respected.
    """
    if media.media_type != "series" or media.sonarr_id is None:
        raise ValueError("Only TV shows matched to Sonarr can be reset.")
    existing = _active_candidate(media.id)
    if existing and existing.state in {"APPROVED", "EXECUTING"}:
        raise ValueError("A cleanup for this show is already running.")
    policy, _ = resolved_policy(media)
    size = reclaimable_bytes(media.parts, policy)
    item = _candidate_input(media)
    now = utcnow()
    if existing:
        # A pending cleanup proposal is replaced by the reset.
        existing.state = "SUPERSEDED"
        existing.scheduled_delete_at = None
    candidate = PurgeCandidate(
        media_identity_id=media.id,
        state="APPROVED",
        reason_code=MANUAL_RESET,
        reason_text=(
            f"Reset {media.title} to its always-keep episodes from the title page."
        ),
        reclaimable_bytes=size,
        last_activity_snapshot=item.last_played_at,
        approved_at=now,
    )
    db.session.add(candidate)
    db.session.flush()
    audit(
        "reset_requested",
        media.id,
        f"Reset to minimum requested for {media.title}.",
        f"reset:{candidate.id}",
        candidate_id=candidate.id,
    )
    db.session.commit()
    return candidate, execute_approved(candidate.id)


def _finish_reset(media, policy, arr, action, unmonitored):
    """After a reset's files are gone: nothing more downloads until someone
    watches again. All seasons are unmonitored (Sonarr then unmonitors their
    episodes too, and adds later episodes unmonitored), the always-keep episodes
    are monitored again, and viewing before now stops counting for acquisition."""
    from .policy import retained

    seasons = arr.unmonitor_seasons(media.sonarr_id)
    trimmed = sorted(
        part.sonarr_episode_id
        for part in media.parts
        if part.kind == "episode"
        and part.sonarr_episode_id is not None
        and not retained(part, policy)
    )
    kept = sorted(
        part.sonarr_episode_id
        for part in media.parts
        if part.kind == "episode"
        and part.sonarr_episode_id is not None
        and retained(part, policy)
    )
    arr.set_episodes_monitored(trimmed, False)
    arr.set_episodes_monitored(kept, True)
    action.payload_json = action.payload_json | {
        "unmonitored": sorted(set(unmonitored) | set(trimmed)),
        "seasons_unmonitored": seasons,
        "kept_monitored": kept,
    }
    media.viewing_reset_at = utcnow()
    db.session.commit()


def execute_approved(candidate_id):
    from .services import process_pending_events

    process_pending_events()
    candidate = db.session.get(PurgeCandidate, candidate_id)
    if not candidate:
        raise ValueError("Unknown candidate")
    db.session.refresh(candidate)
    if candidate.state != "APPROVED":
        return "blocked"
    reason = _validate_deletion(candidate)
    if reason == PENDING_EVENTS_REASON:
        # Stay approved; resume_approved() retries once events are processed.
        audit(
            "delete_deferred",
            candidate.media_identity_id,
            reason,
            f"deferred:{candidate.id}:{candidate.candidate_revision}",
            candidate_id=candidate.id,
        )
        db.session.commit()
        return "deferred"
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


def _playback_preempts_delete(candidate, action, *, deleted_any=False):
    """Drain newly received events before the next destructive request."""
    from .services import process_pending_events

    process_pending_events()
    db.session.refresh(candidate)
    pending = db.session.query(LifecycleEvent.id).filter_by(processed_at=None).first()
    if candidate.state == "EXECUTING" and not pending:
        return False
    if pending:
        candidate.state = "BLOCKED"
    action.state = "FAILED_FINAL" if deleted_any else "CANCELLED"
    action.last_error = (
        "Unprocessed playback events require reconciliation before deletion"
        if pending
        else (
            "Playback or review changed the candidate during partial TV cleanup; "
            "manual review is required"
            if deleted_any
            else "Playback or review cancelled deletion before the external call"
        )
    )
    audit(
        "delete_interrupted",
        candidate.media_identity_id,
        action.last_error,
        f"delete-interrupted:{action.id}",
        candidate_id=candidate.id,
    )
    db.session.commit()
    return True


def execute_action(action_id):
    started = time.monotonic()
    result = _execute_action(action_id)
    if result != "skipped":
        action = db.session.get(LifecycleAction, action_id)
        log_operation(
            action.action_type,
            result,
            level=logging.WARNING if result in {"failed", "unknown"} else logging.INFO,
            action_id=action.id,
            media_identity_id=action.media_identity_id,
            candidate_id=action.candidate_id,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    return result


def _execute_action(action_id):
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
            if action.payload_json.get("monitor_series"):
                action.payload_json = action.payload_json | {
                    "series_monitored": arr.monitor_series(
                        action.payload_json["sonarr_id"]
                    )
                }
                media.arr_monitored = True
            # Record what actually changed; most listed episodes are often
            # monitored already, and History should not overstate the change.
            newly_monitored = [
                episode_id
                for episode_id in action.payload_json.get("episode_ids", [])
                if arr.monitor_episode(episode_id)
            ]
            action.payload_json = action.payload_json | {
                "newly_monitored": newly_monitored
            }
            if action.payload_json.get("whole_season"):
                action.payload_json = action.payload_json | {
                    "season_monitored": arr.monitor_season(
                        action.payload_json["sonarr_id"], action.payload_json["season"]
                    )
                }
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
        reason = _validate_deletion(candidate, states=("EXECUTING",))
        if reason:
            candidate.state = "BLOCKED"
            action.state = "CANCELLED"
            action.last_error = reason
            db.session.commit()
            return "blocked"
        try:
            if media.media_type == "movie":
                arr = client("radarr")
                if _playback_preempts_delete(candidate, action):
                    return "blocked"
                arr.delete_movie(media.radarr_id)
                for part in media.parts:
                    part.has_file = False
            else:
                arr = client("sonarr")
                policy, _ = resolved_policy(media)
                target_files = _tv_file_targets(media, policy, arr)
                _snapshot_watch_state(media)
                deleted_any = False
                unmonitored = []
                for file_id in sorted(target_files):
                    if _playback_preempts_delete(
                        candidate, action, deleted_any=deleted_any
                    ):
                        return "blocked"
                    # Unmonitor first: a monitored episode without a file is
                    # "missing" to Sonarr, and its next missing-episode search
                    # would download the trimmed episodes straight back.
                    for part in media.parts:
                        if (
                            part.kind == "episode"
                            and part.arr_file_id == file_id
                            and part.sonarr_episode_id is not None
                            and arr.unmonitor_episode(part.sonarr_episode_id)
                        ):
                            unmonitored.append(part.sonarr_episode_id)
                    action.payload_json = action.payload_json | {
                        "unmonitored": sorted(unmonitored)
                    }
                    arr.delete_episode_file(file_id)
                    deleted_any = True
                    for part in media.parts:
                        if part.kind == "episode" and part.arr_file_id == file_id:
                            part.has_file = False
                    db.session.commit()
                if candidate.reason_code == MANUAL_RESET:
                    _finish_reset(media, policy, arr, action, unmonitored)
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
                    for part in media.parts:
                        part.has_file = False
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
    from .services import process_pending_events

    process_pending_events()
    now = utcnow()
    rows = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state.in_(["LEAVING_SOON", "REVIEW"]),
            PurgeCandidate.scheduled_delete_at <= now,
        )
        .all()
    )
    expired = 0
    for candidate in rows:
        db.session.refresh(candidate)
        if candidate.state not in {"LEAVING_SOON", "REVIEW"}:
            continue
        candidate.state = "APPROVED"
        candidate.approved_at = now
        db.session.commit()
        execute_approved(candidate.id)
        expired += 1
    return expired


def resume_approved():
    """Retry approvals whose execution was deferred or interrupted before starting."""
    started = (
        db.session.query(LifecycleAction.candidate_id)
        .filter(
            LifecycleAction.candidate_id.isnot(None),
            LifecycleAction.action_type.in_(["delete_media", "would_delete"]),
        )
        .scalar_subquery()
    )
    rows = (
        db.session.query(PurgeCandidate.id)
        .filter(PurgeCandidate.state == "APPROVED", PurgeCandidate.id.notin_(started))
        .all()
    )
    results = [execute_approved(row.id) for row in rows]
    return sum(result != "deferred" for result in results)


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
