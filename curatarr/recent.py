"""Overview's Recently watched and Recently added lists.

Recently watched is Jellyfin's own activity log: every time someone stops
playing something, with the device. A box per person, each play a line, as
the household sees it in Jellyfin. Recently added is when each file arrived
(Sonarr and Radarr); a show's new episodes are one line, so a season pack
doesn't fill the list.
"""

import re
from datetime import datetime, timedelta

from flask import current_app

from . import db, ignored
from .integrations import IntegrationError
from .models import Library, MediaIdentity, MediaPart, utcnow
from .services import _as_datetime, account_names, client, episode_label

WINDOW = timedelta(days=14)
SHOWN = 10
ADDED_SHOWN = 16  # Overview's poster reel
# Opening something by mistake, or to check it plays, isn't watching it.
SHORTEST_PLAY = timedelta(minutes=2)
FRESH_FOR = timedelta(
    minutes=2
)  # Overview reloads often; the log is re-read this often
# "Sam is playing / has finished playing Heat on Living Room TV": Jellyfin writes the
# log in the server's language; elsewhere the title comes from the library
# and the device is left out.
SAID = re.compile(
    r"^.+? (?:has finished playing|is playing) (?P<item>.+) on (?P<device>.+)$"
)


def _norm(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


def watched(now=None, shown=SHOWN) -> list[dict]:
    """Each person's plays over the last two weeks, the most recent viewer
    first: [{"name", "at", "plays": [{"title", "media_id", "episode",
    "device", "at", "finished"}]}], newest first, up to ``shown`` each. []
    when Jellyfin can't be asked."""
    cache = current_app.extensions.setdefault("curatarr_recently_watched", {})
    now = now or utcnow()
    if cache.get("at") and now - cache["at"] < FRESH_FOR and cache["shown"] == shown:
        return cache["people"]
    try:
        jellyfin = client("jellyfin")
        people = _people(
            jellyfin,
            ignored.drop(jellyfin.activity_log(now - WINDOW), jellyfin, key="ItemId"),
            shown,
        )
    except (IntegrationError, AttributeError, TypeError, ValueError, KeyError):
        return cache.get("people", [])
    cache.update(at=now, shown=shown, people=people)
    return people


def _people(jellyfin, log, shown) -> list[dict]:
    names = {_norm(i): n for i, n in account_names().items()}
    entries = sorted(
        (e for e in log if isinstance(e, dict) and e.get("Date")),
        # A start before its stop when they share a moment.
        key=lambda e: (e["Date"], e.get("Type") != "VideoPlayback"),
    )
    started, found = {}, []
    for entry in entries:
        user, item = _norm(entry.get("UserId")), _norm(entry.get("ItemId"))
        if user not in names or not item:
            continue
        at = _when(entry["Date"])
        said = SAID.match(entry.get("Name") or "")
        if entry.get("Type") == "VideoPlayback":
            started[(user, item)] = (at, said)
        elif entry.get("Type") == "VideoPlaybackStopped":
            began, _said = started.pop((user, item), (None, None))
            if began and at - began < SHORTEST_PLAY:
                continue
            found.append((user, item, at, said))
    # Started with no stop logged: still playing, or the app dropped off.
    found.extend((user, item, at, said) for (user, item), (at, said) in started.items())
    plays = {}
    for user, item, at, said in sorted(found, key=lambda f: f[2]):
        lines = plays.setdefault(user, [])
        if lines and lines[-1]["item"] == item:
            said = said or (lines[-1]["said"], lines[-1]["device"])
            lines.pop()  # paused and picked up again: one line
        if isinstance(said, re.Match):
            said = (said["item"], said["device"])
        said = said or (None, None)
        lines.append({"item": item, "at": at, "said": said[0], "device": said[1]})
    for lines in plays.values():
        lines.reverse()
        del lines[shown:]
    library = _library({line["item"] for lines in plays.values() for line in lines})
    people = []
    for user, lines in plays.items():
        try:
            finished = jellyfin.played(user, {line["item"] for line in lines})
        except IntegrationError:
            finished = None
        finished = {_norm(i) for i in finished} if finished is not None else None
        shown_lines = []
        for line in lines:
            known = library.get(line["item"])
            title = known["title"] if known else line["said"]
            if not title:
                continue
            episode = None
            if known and known["episode"]:
                name = (line["said"] or "").removeprefix(known["title"] + " - ")
                episode = known["episode"] + (
                    f" · {name}" if name and name != line["said"] else ""
                )
            shown_lines.append(
                {
                    "title": title,
                    "media_id": known["media_id"] if known else None,
                    "episode": episode,
                    "device": line["device"],
                    "at": line["at"],
                    "finished": None if finished is None else line["item"] in finished,
                }
            )
        if shown_lines:
            people.append(
                {"name": names[user], "at": shown_lines[0]["at"], "plays": shown_lines}
            )
    return sorted(people, key=lambda p: p["at"], reverse=True)


def _when(text) -> datetime:
    # Jellyfin gives seven decimal places, more than fromisoformat takes.
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(text)).replace("Z", "+00:00")
    return _as_datetime(text) or utcnow()


def _library(items) -> dict[str, dict]:
    """Jellyfin item id -> the film or episode Curatarr knows it as. Both
    keep Jellyfin's ids lower-case without dashes."""
    items, found = list(items), {}
    for start in range(0, len(items), 500):
        chunk = items[start : start + 500]
        for media in db.session.query(MediaIdentity).filter(
            MediaIdentity.jellyfin_id.in_(chunk)
        ):
            found[_norm(media.jellyfin_id)] = {
                "media_id": media.id,
                "title": media.label,
                "episode": None,
            }
        rows = (
            db.session.query(MediaPart, MediaIdentity)
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(MediaPart.kind == "episode", MediaPart.jellyfin_id.in_(chunk))
        )
        for part, media in rows:
            found[_norm(part.jellyfin_id)] = {
                "media_id": media.id,
                "title": media.title,
                "episode": episode_label(part.season_number, part.episode_number),
            }
    return found


def added(now=None, shown=ADDED_SHOWN) -> dict[str, list[dict]]:
    """Films and shows new to the library lately, newest first, up to
    ``shown`` of each: {"films": [...], "shows": [...]}. A show lists how many
    of its episodes came. A file that replaced another (an upgrade, or a copy
    Curatarr replaced) isn't new, so it isn't listed."""
    from .origin import first_copies

    now = now or utcnow()
    since = now - WINDOW
    found = (
        db.session.query(MediaPart, MediaIdentity.media_type)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .filter(
            MediaPart.has_file.is_(True),
            MediaPart.acquired_at >= since,
            MediaIdentity.missing_since.is_(None),
            Library.media_type.in_(("tv", "movies")),  # Library.managed
        )
        .all()
    )
    shows = {part.id: media_type == "series" for part, media_type in found}
    rows = {}
    for part in first_copies([part for part, _type in found]):
        at = _as_datetime(part.acquired_at)
        row = rows.setdefault(
            part.media_identity_id,
            {
                "media_id": part.media_identity_id,
                "at": at,
                "episodes": [],
                "show": shows[part.id],
            },
        )
        row["at"] = max(row["at"], at)
        if part.kind == "episode":
            row["episodes"].append((part.season_number, part.episode_number))
    newest = sorted(rows.values(), key=lambda r: r["at"], reverse=True)
    latest = [r for r in newest if not r["show"]][:shown] + [
        r for r in newest if r["show"]
    ][:shown]
    titles = _titles([r["media_id"] for r in latest])
    result = {"films": [], "shows": []}
    for row in latest:
        if row["media_id"] not in titles:
            continue
        episodes = sorted(row["episodes"], key=lambda e: (e[0] or 0, e[1] or 0))
        result["shows" if row["show"] else "films"].append(
            {
                "media_id": row["media_id"],
                "title": titles[row["media_id"]].label,
                "origin": (titles[row["media_id"]].origin or {}).get("short"),
                "at": row["at"],
                "episodes": len(episodes),
                "first": episode_label(*episodes[0]) if episodes else None,
                "last": episode_label(*episodes[-1]) if episodes else None,
            }
        )
    return result


def _titles(ids) -> dict:
    if not ids:
        return {}
    return {
        media.id: media
        for media in db.session.query(MediaIdentity).filter(MediaIdentity.id.in_(ids))
    }
