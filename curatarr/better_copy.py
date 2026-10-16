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
KINDS = ("file", "dub")


def _result(state, kind):
    return (state.file_check if kind == "file" else state.dub_result) or {}


def _set(state, kind, **values):
    if kind == "file":
        state.file_check = {**(state.file_check or {}), **values}
    else:
        state.dub_result = {**(state.dub_result or {}), **values}


def request(state, kind) -> str:
    """Queue a replacement; the worker replaces only if a better copy exists."""
    from .services import set_setting, setting, utcnow

    if kind not in KINDS:
        raise ValueError(kind)
    queue = [q for q in setting(QUEUE_KEY, []) or [] if q["state"] != state.id]
    queue.append({"state": state.id, "kind": kind})
    set_setting(QUEUE_KEY, queue)
    _set(state, kind, replace_requested=utcnow().isoformat(), no_better=None)
    db.session.commit()
    return (
        "Looking for a better copy first. The file is replaced only if one can be "
        "found, in a few minutes; otherwise it's kept and this page says so."
    )


def pending(state, kind) -> bool:
    return bool(_result(state, kind).get("replace_requested"))


def candidate(releases, current, *, split_film=False):
    """The release Radarr or Sonarr would take once the current file is gone."""
    current = (current or "").lower()
    for release in releases or []:
        title = release.get("title") or ""
        if current and title.lower().startswith(current[:60]):
            continue  # the copy already here
        if split_film and SPLIT.search(title):
            continue  # one more part of a split film
        if all(
            any(gone in reason.lower() for gone in GONE_WITH_THE_FILE)
            for reason in release.get("rejections") or []
        ):
            return release
    return None


def _check(state, kind):
    """(release found or None, how many releases were looked at)."""
    from .services import client

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


def sweep() -> int:
    """Work through queued replacements (worker cycle)."""
    from . import dub_jobs, filecheck_jobs
    from .services import set_setting, setting, utcnow

    queue = list(setting(QUEUE_KEY, []) or [])
    if not queue:
        return 0
    done = 0
    for item in queue[:PER_RUN]:
        state = db.session.get(ChapterState, item["state"])
        rest = [q for q in setting(QUEUE_KEY, []) or [] if q != item]
        if state is None or not pending(state, item["kind"]):
            set_setting(QUEUE_KEY, rest)
            db.session.commit()
            continue
        db.session.rollback()  # ask the indexers outside any transaction
        try:
            found, looked_at = _check(state, item["kind"])
        except IntegrationError:
            continue  # try again next cycle
        state = db.session.get(ChapterState, item["state"])
        _set(state, item["kind"], replace_requested=None)
        set_setting(QUEUE_KEY, rest)
        db.session.commit()
        if found:
            try:
                (filecheck_jobs if item["kind"] == "file" else dub_jobs).replace(state)
            except (IntegrationError, ValueError) as exc:
                db.session.rollback()
                state = db.session.get(ChapterState, item["state"])
                _set(state, item["kind"], replace_error=str(exc))
                db.session.commit()
        else:
            _set(
                state,
                item["kind"],
                no_better={"at": utcnow().isoformat(), "checked": looked_at},
            )
            db.session.commit()
        done += 1
    return done
