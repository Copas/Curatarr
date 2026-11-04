"""Manual recovery for completed downloads that Radarr or Sonarr cannot import."""

import time

from . import db
from .integrations import IntegrationError
from .models import utcnow
from .services import audit, client

SERVICES = ("sonarr", "radarr")


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
                }
            )
    return sorted(found, key=lambda row: (row["service"], row["title"])), errors


def retry(service, queue_id):
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
        "files, because it could not be imported.",
        history_key,
        payload={"service": service, "targets": sorted(target_ids)},
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
