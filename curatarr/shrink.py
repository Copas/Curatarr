"""Shrink instead of delete: swap a big film for a much smaller copy.

A film that's kept but rarely watched doesn't need its biggest copy. Shrink
asks Radarr's indexers for a release at most ``MAX_RATIO`` of the current
size, at the same resolution or (above 1080p) at 1080p, and swaps only when
one exists: the current file is deleted and that release is downloaded, as
Replace does. Until then nothing changes, and when no copy is small enough
the page says so.

When the film's quality profile allows upgrades, Radarr would fetch a big copy
again, so the film is unmonitored; Curatarr's own cleanup works either way.
If the smaller copy hasn't arrived after ``ARRIVAL_DAYS``, the film is
monitored again and Radarr searches for any copy.
"""

from datetime import timedelta

from . import db
from .better_copy import GONE_WITH_THE_FILE, SPLIT
from .integrations import IntegrationError
from .models import MediaIdentity, utcnow

KEY = "shrink"  # {media id: {"requested", "no_smaller", "swapped", "error"}}
MAX_RATIO = 0.5
MIN_BYTES = 4 * 1024**3  # smaller films aren't worth it
ARRIVAL_DAYS = 2


def _all():
    from .services import setting

    return dict(setting(KEY, {}) or {})


def _set(media_id, **values):
    from .services import set_setting

    stored = _all()
    entry = {**(stored.get(media_id) or {}), **values}
    stored[media_id] = {k: v for k, v in entry.items() if v is not None}
    set_setting(KEY, stored)


def state(media):
    return _all().get(media.id) or {}


def status(media):
    """pending, swapping, done, error, no_smaller, or None (offer Shrink)."""
    entry = state(media)
    if entry.get("requested"):
        return "pending"
    if entry.get("swapped"):
        return "done" if entry["swapped"].get("arrived") else "swapping"
    if entry.get("error"):
        return "error"
    if entry.get("no_smaller"):
        return "no_smaller"
    return None


def offered(media):
    """Whether Shrink makes sense for this title."""
    return (
        media.media_type == "movie"
        and media.radarr_id is not None
        and sum(p.size_bytes for p in media.parts if p.has_file) >= MIN_BYTES
    )


def request(media):
    if not offered(media):
        raise ValueError("Only films of 4 GB or more can be shrunk")
    _set(media.id, requested=utcnow().isoformat(), no_smaller=None, error=None)
    db.session.commit()
    return (
        "Looking for a copy half the size or smaller. The film is swapped only "
        "if one is found, in a few minutes; otherwise it stays as it is."
    )


def cancel(media):
    _set(media.id, requested=None)
    db.session.commit()


def dismiss(media):
    _set(media.id, no_smaller=None, error=None)
    db.session.commit()


def _height(release):
    quality = (release.get("quality") or {}).get("quality") or {}
    return quality.get("resolution") or 0


def pick(releases, current_size, current_height):
    """The best release at most MAX_RATIO of the current size, or None."""
    floor = min(current_height or 1080, 1080)
    ok = []
    for release in releases or []:
        size = release.get("size") or 0
        title = release.get("title") or ""
        if not 0 < size <= current_size * MAX_RATIO:
            continue
        if size < 700 * 1024**2 or SPLIT.search(title):
            continue  # a sample, or one part of a split film
        if _height(release) < floor:
            continue
        if not all(
            any(gone in reason.lower() for gone in GONE_WITH_THE_FILE)
            for reason in release.get("rejections") or []
        ):
            continue
        ok.append(release)
    # Highest resolution, then the largest that still saves enough.
    return max(ok, key=lambda r: (_height(r), r.get("size") or 0), default=None)


def _look(media):
    """(Radarr's film, the release to swap to or None, releases looked at)."""
    from .services import client

    radarr = client("radarr")
    movie = radarr.movie(media.radarr_id) or {}
    current = movie.get("movieFile") or {}
    height = ((current.get("quality") or {}).get("quality") or {}).get("resolution")
    releases = radarr.releases(media.radarr_id)
    found = pick(releases, current.get("size") or 0, height)
    return movie, (found if current.get("id") else None), len(releases or [])


def _swap(media, movie, found):
    """Delete the big file and download the small one."""
    from .services import audit, client

    radarr = client("radarr")
    current = movie["movieFile"]
    size = current.get("size") or 0
    unmonitored = False
    try:
        profile = radarr.quality_profile(movie.get("qualityProfileId")) or {}
        if profile.get("upgradeAllowed") and movie.get("monitored"):
            radarr.set_monitored(media.radarr_id, False)
            unmonitored = True
        radarr.delete_movie_file(current["id"])
    except IntegrationError as exc:
        if unmonitored:
            radarr.set_monitored(media.radarr_id, True)
        exc.nothing_changed = True
        raise
    try:
        radarr.grab(found)
    except IntegrationError:
        # The big copy is gone; have Radarr find any copy rather than none.
        if unmonitored:
            radarr.set_monitored(media.radarr_id, True)
        radarr.search_movie(media.radarr_id)
        raise
    finally:
        for part in media.parts:
            part.has_file = False
    swapped = {
        "at": utcnow().isoformat(),
        "from": size,
        "to": found.get("size"),
        "release": found.get("title"),
        "unmonitored": unmonitored,
    }
    audit(
        "shrunk",
        media.id,
        f"Swapped a {size / 1024**3:.1f} GB copy for a {found['size'] / 1024**3:.1f} GB one.",
        f"shrink:{media.id}:{swapped['at']}",
        payload=swapped,
    )
    _rescue(media)
    return swapped


def _rescue(media):
    """A film being shrunk is being kept: take it out of the review queue."""
    from .lifecycle import rescue_candidate
    from .services import _active_candidate

    candidate = _active_candidate(media.id)
    if candidate is not None and candidate.state != "EXECUTING":
        rescue_candidate(candidate, "Shrunk to a smaller copy instead.")


def _follow_up(media, entry):
    """After a swap: done when the file arrives; otherwise fall back."""
    from .services import _as_datetime, client

    swapped = entry["swapped"]
    if swapped.get("arrived") or swapped.get("fell_back"):
        return
    radarr = client("radarr")
    movie = radarr.movie(media.radarr_id) or {}
    if movie.get("hasFile"):
        _set(media.id, swapped=swapped | {"arrived": utcnow().isoformat()})
        return
    if _as_datetime(swapped["at"]) + timedelta(days=ARRIVAL_DAYS) > utcnow():
        return
    if swapped.get("unmonitored"):
        radarr.set_monitored(media.radarr_id, True)
    radarr.search_movie(media.radarr_id)
    _set(media.id, swapped=swapped | {"fell_back": utcnow().isoformat()})


def sweep():
    """Worker cycle: one lookup per run (it asks every indexer), plus follow-ups."""
    stored = _all()
    if not stored:
        return 0
    done = 0
    for media_id, entry in stored.items():
        media = db.session.get(MediaIdentity, media_id)
        if media is None:
            continue
        if entry.get("swapped"):
            try:
                _follow_up(media, entry)
                db.session.commit()
            except IntegrationError:
                db.session.rollback()
            continue
        if not entry.get("requested") or done:
            continue
        db.session.rollback()  # ask the indexers outside any transaction
        try:
            movie, found, looked_at = _look(media)
        except IntegrationError:
            db.session.rollback()
            continue  # try again next cycle
        if not state(media).get("requested"):
            continue  # cancelled while the indexers were asked
        done += 1
        if found is None:
            _set(
                media.id,
                requested=None,
                no_smaller={"at": utcnow().isoformat(), "checked": looked_at},
            )
            db.session.commit()
            continue
        _set(media.id, requested=None)
        db.session.commit()
        try:
            _set(media.id, swapped=_swap(media, movie, found))
        except IntegrationError as exc:
            db.session.rollback()
            _set(
                media.id,
                error="Radarr couldn't be reached; nothing was changed."
                if getattr(exc, "nothing_changed", False)
                else "Radarr didn't take the smaller copy; it's searching for any copy instead.",
            )
        db.session.commit()
    return done


def view(media):
    """What the page shows: whether Shrink is offered, and where it's up to."""
    return {"offered": offered(media), "status": status(media), "entry": state(media)}
