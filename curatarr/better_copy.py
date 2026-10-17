"""Replace only when there's something to replace with.

Replace used to blocklist the release, delete the file and then ask Radarr or
Sonarr to search. When nothing better existed, that left no copy at all: a
Swedish film whose only English versions are split in two parts, a film whose
only English releases were the ones just rejected.

Now Replace asks first. The request is queued (searching every indexer takes
a minute or two, longer than a page may wait), and the worker looks through
the releases for one that would be taken once the current file is gone: not
the release already here, and not one more part of a split film. Only then is
the file replaced; otherwise it stays, and the page says nothing better was
available and when that was checked.
"""

import re

from . import db
from .integrations import IntegrationError
from .models import ChapterState

QUEUE_KEY = "replace_queue"
PER_RUN = 2  # each check asks every indexer
# Rejections that stop applying once the current file is deleted.
GONE_WITH_THE_FILE = (
    "existing file",
    "meets cutoff",
    "does not allow upgrades",
    "not an upgrade",
    "not a custom format upgrade",
)
SPLIT = re.compile(
    r"(?i)(?:^|[ ._\-\[(])(part|pt|cd|disc|disk)[ ._\-]?(\d|i{1,3}|iv)(?=[ ._\-\])]|$)"
)
KINDS = ("file", "dub", "version")


def _target(item_id, kind):
    """What a queued request is about: a file's ChapterState, or a show."""
    from .models import MediaIdentity

    model = MediaIdentity if kind == "version" else ChapterState
    return db.session.get(model, item_id)


def _result(target, kind):
    if kind == "version":
        from .services import setting
        from .wrong_version import KEY

        return (setting(KEY, {}) or {}).get(target.id) or {}
    return (target.file_check if kind == "file" else target.dub_result) or {}


def _set(target, kind, **values):
    if kind == "version":
        from .services import set_setting, setting
        from .wrong_version import KEY

        stored = dict(setting(KEY, {}) or {})
        stored[target.id] = {**(stored.get(target.id) or {}), **values}
        set_setting(KEY, stored)
    elif kind == "file":
        target.file_check = {**(target.file_check or {}), **values}
    else:
        target.dub_result = {**(target.dub_result or {}), **values}


def _unqueue(target_id):
    from .services import set_setting, setting

    set_setting(
        QUEUE_KEY, [q for q in setting(QUEUE_KEY, []) or [] if q["state"] != target_id]
    )


def request(target, kind, *, auto=False) -> str:
    """Queue a replacement; the worker replaces only if a better copy exists.

    ``auto``: started by an automatic fix (autofix.py), which drops the item
    quietly if nothing better turns up."""
    from .services import set_setting, setting, utcnow

    if kind not in KINDS:
        raise ValueError(kind)
    queue = [q for q in setting(QUEUE_KEY, []) or [] if q["state"] != target.id]
    queue.append({"state": target.id, "kind": kind})
    set_setting(QUEUE_KEY, queue)
    _set(
        target,
        kind,
        replace_requested=utcnow().isoformat(),
        no_better=None,
        replace_auto=auto,
    )
    db.session.commit()
    return (
        "Looking for a better copy first. The file is replaced only if one can be "
        "found, in a few minutes; otherwise it's kept and this page says so."
    )


def cancel(target, kind) -> None:
    """Take back a Replace that hasn't been decided yet."""
    _unqueue(target.id)
    _set(target, kind, replace_requested=None)
    db.session.commit()


def pending(target, kind) -> bool:
    return bool(_result(target, kind).get("replace_requested"))


def status(target, kind):
    """What a row's Replace area shows: "pending" (looking), "no_better" (looked,
    nothing to replace it with) or None (offer Replace)."""
    result = _result(target, kind)
    if result.get("replace_requested"):
        return "pending"
    if result.get("no_better"):
        return "no_better"
    return None


def was_automatic(target, kind) -> bool:
    return bool(_result(target, kind).get("replace_auto"))


def give_up(target, kind) -> None:
    """An automatic Replace found nothing better: stop listing it, quietly."""
    from . import dub_jobs, filecheck_jobs, wrong_version
    from .services import audit

    found = _result(target, kind).get("no_better") or {}
    media = target if kind == "version" else target.media
    {"file": filecheck_jobs, "dub": dub_jobs, "version": wrong_version}[kind].dismiss(
        target
    )
    audit(
        "auto_fix_gave_up",
        media.id,
        f"No better copy was available ({found.get('checked', 0)} releases "
        "checked); kept the file and stopped trying.",
        f"auto-gave-up:{kind}:{target.id}:{found.get('at')}",
        payload={"kind": kind},
    )
    db.session.commit()


def checked_at(target, kind):
    """When "no better copy" was decided, for the page."""
    return (_result(target, kind).get("no_better") or {}).get("at")


def candidate(releases, current, *, split_film=False, reject=None):
    """The release Radarr or Sonarr would take once the current file is gone."""
    current = (current or "").lower()
    for release in releases or []:
        title = release.get("title") or ""
        if current and title.lower().startswith(current[:60]):
            continue  # the copy already here
        if split_film and SPLIT.search(title):
            continue  # one more part of a split film
        if reject and reject(title):
            continue
        if all(
            any(gone in reason.lower() for gone in GONE_WITH_THE_FILE)
            for reason in release.get("rejections") or []
        ):
            return release
    return None


def _check_version(media):
    """A release of the right show for one of the flagged episodes."""
    from .services import client

    verdict = _result(media, "version").get("verdict") or {}
    file_ids = {f["id"] for f in verdict.get("files") or []}
    names = [(f.get("name") or "").lower()[:40] for f in verdict.get("files") or []]
    year = str(verdict.get("year") or "")
    part = next(
        (
            p
            for p in media.parts
            if p.kind == "episode" and p.arr_file_id in file_ids and p.sonarr_episode_id
        ),
        None,
    )
    if part is None:
        return None, 0
    releases = client("sonarr").releases(part.sonarr_episode_id)

    def wrong(title):
        title = title.lower()
        return bool(year and year in title) or any(
            n and title.startswith(n) for n in names
        )

    return candidate(releases, None, reject=wrong), len(releases or [])


def _check(state, kind):
    """(release found or None, how many releases were looked at)."""
    from .services import client

    if kind == "version":
        return _check_version(state)
    part, media = state.part, state.media
    split_film = kind == "file" and any(
        "split into parts" in (issue.get("text") or "")
        for issue in (state.file_check or {}).get("issues") or []
    )
    if part.kind == "movie_file":
        arr = client("radarr")
        current = ((arr.movie(media.radarr_id) or {}).get("movieFile") or {}).get(
            "sceneName"
        )
        releases = arr.releases(media.radarr_id)
    else:
        arr = client("sonarr")
        current = None
        grabs = arr.episode_grabs(part.sonarr_episode_id)
        if grabs:
            current = grabs[0].get("sourceTitle")
        releases = arr.releases(part.sonarr_episode_id)
    return candidate(releases, current, split_film=split_film), len(releases or [])


def _replace(target, kind):
    from . import dub_jobs, filecheck_jobs, wrong_version

    if kind == "version":
        return wrong_version.replace(target)
    return (filecheck_jobs if kind == "file" else dub_jobs).replace(target)


def sweep() -> int:
    """Work through queued replacements (worker cycle)."""
    from .services import setting, utcnow

    queue = list(setting(QUEUE_KEY, []) or [])
    if not queue:
        return 0
    done = 0
    for item in queue[:PER_RUN]:
        kind = item.get("kind", "file")
        target = _target(item["state"], kind)
        if target is None or not pending(target, kind):
            _unqueue(item["state"])
            db.session.commit()
            continue
        db.session.rollback()  # ask the indexers outside any transaction
        try:
            found, looked_at = _check(target, kind)
        except IntegrationError:
            continue  # try again next cycle
        target = _target(item["state"], kind)
        if target is None or not pending(target, kind):
            continue  # cancelled while the indexers were asked
        _set(target, kind, replace_requested=None)
        _unqueue(item["state"])
        db.session.commit()
        if found:
            try:
                _replace(target, kind)
            except (IntegrationError, ValueError) as exc:
                db.session.rollback()
                target = _target(item["state"], kind)
                _set(target, kind, replace_error=str(exc))
                db.session.commit()
        else:
            _set(
                target,
                kind,
                no_better={"at": utcnow().isoformat(), "checked": looked_at},
            )
            db.session.commit()
            if was_automatic(target, kind):
                give_up(target, kind)
        done += 1
    return done
