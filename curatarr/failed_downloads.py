"""Manual recovery for completed downloads that Radarr or Sonarr cannot import."""

import time
from datetime import timedelta

from . import db
from .integrations import IntegrationError
from .models import utcnow
from .services import audit, client

SERVICES = ("sonarr", "radarr")
WAITING_KEY = "failed_downloads_waiting"  # {"count", "at"}: for the Problems badge
CHECK_EVERY = timedelta(minutes=10)
AUTO_PER_WEEK = 2  # automatic retries for the same episode or film, then a person
AUTO_PER_RUN = 2


def _stuck(row):
    return (
        row.get("status") == "completed"
        and row.get("trackedDownloadStatus") == "warning"
        and bool(row.get("downloadId"))
    )


def _has_library_file(service, group):
    """Whether a target already has a file, from the queue's own details: a
    failed upgrade, or a season pack that partly imported."""
    key = "episode" if service == "sonarr" else "movie"
    return any((row.get(key) or {}).get("hasFile") for row in group)


def _media_id(service, group):
    """The title in Curatarr these rows belong to, for History, if it's known."""
    from .models import MediaIdentity

    key = "seriesId" if service == "sonarr" else "movieId"
    field = MediaIdentity.sonarr_id if service == "sonarr" else MediaIdentity.radarr_id
    title_ids = {row.get(key) for row in group} - {None}
    if len(title_ids) != 1:
        return None
    return db.session.query(MediaIdentity.id).filter(field == title_ids.pop()).scalar()


def _dry_run_for(service, group):
    """Use the title's deletion policy; unknown titles use the global policy."""
    from .models import MediaIdentity
    from .policy import effective_policy
    from .services import resolved_policy, setting

    key = "seriesId" if service == "sonarr" else "movieId"
    title_ids = {row.get(key) for row in group}
    if len(title_ids) != 1 or None in title_ids:
        raise ValueError("The download no longer belongs to one clear title")
    field = MediaIdentity.sonarr_id if service == "sonarr" else MediaIdentity.radarr_id
    media = db.session.query(MediaIdentity).filter(field == title_ids.pop()).first()
    if media:
        return bool(resolved_policy(media)[0]["dry_run"])
    media_type = "series" if service == "sonarr" else "movie"
    return bool(
        effective_policy(media_type, setting("global_policy", {}))[0]["dry_run"]
    )


def review():
    """One review card per download, even when a season pack has many rows."""
    found, errors = [], []
    for service in SERVICES:
        try:
            rows = client(service).queue_records(quick=True)
        except IntegrationError as exc:
            if "not configured" not in str(exc):
                errors.append(f"{service.title()}: {exc}")
            continue
        groups = {}
        for row in rows:
            if _stuck(row):
                groups.setdefault(row["downloadId"], []).append(row)
        for group in groups.values():
            if len({row.get("title") for row in group}) != 1:
                continue
            try:
                blocked_reason = (
                    "Dry run is on for this library"
                    if _dry_run_for(service, group)
                    else "A library file is already here; review this one by hand"
                    if _has_library_file(service, group)
                    else None
                )
            except ValueError as exc:
                blocked_reason = str(exc)
            messages = sorted(
                {
                    text
                    for row in group
                    for status in row.get("statusMessages") or []
                    for text in status.get("messages") or []
                }
            )
            found.append(
                {
                    "service": service,
                    "queue_id": group[0]["id"],
                    "title": group[0].get("title") or "Unknown download",
                    "count": len(group),
                    "messages": messages,
                    "blocked_reason": blocked_reason,
                    "targets": sorted(
                        {
                            row.get("episodeId" if service == "sonarr" else "movieId")
                            for row in group
                        }
                        - {None}
                    ),
                }
            )
    found.sort(key=lambda row: (row["service"], row["title"]))
    if not errors:
        _remember(len(found))
    return found, errors


def _remember(count):
    from .services import set_setting

    set_setting(WAITING_KEY, {"count": count, "at": utcnow().isoformat()})
    db.session.commit()


def count() -> int:
    """Stuck downloads found on the last look (no Sonarr or Radarr call)."""
    from .services import setting

    return int((setting(WAITING_KEY, {}) or {}).get("count", 0))


def _retried_lately(service, targets) -> int:
    """Automatic retries in the last week touching any of these targets."""
    from .models import LifecycleAction

    since = utcnow() - timedelta(days=7)
    rows = db.session.query(LifecycleAction.payload_json).filter(
        LifecycleAction.action_type == "failed_download_removed",
        LifecycleAction.created_at >= since,
    )
    return sum(
        1
        for (payload,) in rows
        if (payload or {}).get("auto")
        and payload.get("service") == service
        and set(payload.get("targets") or []) & set(targets)
    )


def sweep() -> int:
    """Worker cycle, every few minutes: count stuck downloads for the Problems
    badge and, with Settings → Automatic on, blocklist them and search again.
    The same episode or film is retried at most twice a week, so a release
    that keeps failing comes back to a person. Returns how many were retried."""
    from . import autofix
    from .services import _as_datetime, setting

    last = _as_datetime((setting(WAITING_KEY, {}) or {}).get("at"))
    if last and utcnow() - last < CHECK_EVERY:
        return 0
    found, errors = review()
    if errors:
        _remember(count())  # try again later, keeping the last count
        return 0
    if not autofix.automatic("downloads"):
        return 0
    retried = 0
    for row in found:
        if retried >= AUTO_PER_RUN or row["blocked_reason"]:
            continue
        if _retried_lately(row["service"], row["targets"]) >= AUTO_PER_WEEK:
            continue
        try:
            retry(row["service"], row["queue_id"], auto=True)
        except (ValueError, IntegrationError):
            db.session.rollback()
            continue
        retried += 1
    if retried:
        _remember(max(len(found) - retried, 0))
    return retried


def retry(service, queue_id, *, auto=False):
    """Blocklist one stuck download, remove its client files, then search again.

    The queue is read again before acting. A stale card cannot remove a newer
    download that happens to occupy the same episode or film.
    """
    if service not in SERVICES:
        raise ValueError("Unknown download service")
    arr = client(service)
    rows = arr.queue_records()
    selected = next((row for row in rows if row.get("id") == queue_id), None)
    if selected is None or not _stuck(selected):
        raise ValueError("That completed download is no longer waiting to import")
    group = [row for row in rows if row.get("downloadId") == selected["downloadId"]]
    if any(
        not _stuck(row) or row.get("title") != selected.get("title") for row in group
    ):
        raise ValueError("The download changed; reload the page and review it again")

    key = "episodeId" if service == "sonarr" else "movieId"
    target_ids = {row.get(key) for row in group}
    if not target_ids or None in target_ids:
        raise ValueError("The download has no clear episode or film to search for")
    for target_id in target_ids:
        item = arr.episode(target_id) if service == "sonarr" else arr.movie(target_id)
        if item.get("hasFile") or item.get("movieFile"):
            raise ValueError(
                "A library file is already present; review this download by hand"
            )
    if _dry_run_for(service, group):
        raise ValueError(
            "Dry run is on for this library, so Curatarr won't remove the "
            "download files. Turn dry run off, or handle it in Sonarr or Radarr"
        )

    arr.remove_failed_download(queue_id)
    title = selected.get("title") or "Unknown download"
    media_id = _media_id(service, group)
    history_key = (
        f"download-retry:{service}:{selected['downloadId']}:{utcnow().isoformat()}"
    )
    audit(
        "failed_download_removed",
        media_id,
        f"Blocklisted “{title}” in {service.title()} and removed its download "
        "files, because it could not be imported" + (" (automatic)." if auto else "."),
        history_key,
        payload={"service": service, "targets": sorted(target_ids), "auto": auto},
    )
    db.session.commit()

    def search_failed(message):
        audit(
            "failed_download_search_failed",
            media_id,
            f"{message} after removing “{title}”: search for it in {service.title()}.",
            f"{history_key}:search",
            payload={"service": service, "targets": sorted(target_ids)},
            state="FAILED",
        )
        db.session.commit()

    for _ in range(5):
        if not any(
            row.get("downloadId") == selected["downloadId"]
            for row in arr.queue_records()
        ):
            break
        time.sleep(1)
    else:
        search_failed("No search was started")
        raise IntegrationError(
            "The download is still in the queue; no search was started"
        )

    try:
        if service == "sonarr":
            arr.episode_search(sorted(target_ids))
        else:
            for movie_id in sorted(target_ids):
                arr.search_movie(movie_id)
    except IntegrationError as exc:
        search_failed("The search could not start")
        raise IntegrationError(
            "The download was blocklisted and removed, but the search could "
            f"not start in {service.title()}"
        ) from exc
    return (
        f"Blocklisted and removed the download. Asked {service.title()} to search "
        "for another release; it may find none that your quality profile accepts."
    )
