"""Mark a film or episode watched when someone stops it in the credits.

Jellyfin counts something as watched only when it's stopped past 90% of its
length. A film's end credits often run longer than that last tenth, so a film
everyone finished stays in Continue Watching, a few minutes from the end.

Every few minutes Curatarr looks at each person's Continue Watching. Something
counts as finished when it was stopped:

- in its end credits (Intro Skipper's credits marker, in the last quarter), or
- after the last line of dialogue, with only a few minutes left (twelve for a
  film, three for an episode). The dialogue comes from the file's subtitles:
  Curatarr's own transcript, or an English text subtitle that runs to near
  the end (forced and partial ones don't).

It's marked watched for that person only, which also clears the resume point
(Settings → Enhance, on by default). Anything still playing is left alone,
and something stopped mid-film is never touched: those are Jellyfin's to keep.
"""

import re
from datetime import timedelta
from pathlib import PurePath

from . import db
from .integrations import IntegrationError
from .models import ChapterState, MediaIdentity, MediaPart, utcnow

KEY = "finished_at_credits"
EVERY = timedelta(minutes=5)
TICKS = 10_000_000
CREDITS_GRACE = 30.0  # seconds before the credits marker that still count
CREDITS_FROM = 0.75  # a credits marker earlier than this share is an opening's
FILM_TAIL = 12 * 60.0  # after the last dialogue, this little left is the ending
EPISODE_TAIL = 3 * 60.0
FULL_SUBTITLES = 0.7  # subtitles ending earlier than this share are partial
CUE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{3})"
)


def enabled() -> bool:
    from .enrichment import options

    try:
        return bool(options()["finish_at_credits"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def _norm(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


def last_dialogue(path) -> float | None:
    """When the last subtitle cue in an SRT or VTT file ends, in seconds."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    last = None
    for match in CUE.finditer(text):
        h, m, s, ms = (int(x) for x in match.groups()[4:])
        end = h * 3600 + m * 60 + s + ms / 1000
        last = end if last is None else max(last, end)
    return last


def _subtitle_files(part, state) -> list[str]:
    from .subtitles import sidecar_english

    files = []
    if state and state.subtitle_path:
        files.append(state.subtitle_path)
    if part.file_path:
        files.extend(sidecar_english(part.file_path, text_only=True))
    return files


def dialogue_ends(part, state, runtime) -> float | None:
    """The last dialogue from the first subtitles that cover the whole file."""
    for path in _subtitle_files(part, state):
        end = last_dialogue(path)
        if end and end >= runtime * FULL_SUBTITLES:
            return end
    return None


def credits_start(segments, runtime) -> float | None:
    starts = [
        s["StartTicks"] / TICKS
        for s in segments or []
        if s.get("Type") == "Outro" and s.get("StartTicks") is not None
    ]
    starts = [s for s in starts if s >= runtime * CREDITS_FROM]
    return min(starts) if starts else None


def finished_reason(position, runtime, credits, dialogue, episode) -> str | None:
    """Why stopping at ``position`` (seconds) means it was watched, or None."""
    if runtime <= 0 or position <= 0 or position >= runtime:
        return None
    if credits is not None and position >= credits - CREDITS_GRACE:
        return "in the credits"
    tail = EPISODE_TAIL if episode else FILM_TAIL
    if dialogue is not None and position >= dialogue and runtime - position <= tail:
        return "after the last line of dialogue"
    return None


def _part_for(item):
    item_id = _norm(item.get("Id"))
    if item.get("Type") == "Episode":
        return (
            db.session.query(MediaPart)
            .filter(MediaPart.jellyfin_id.in_([item_id, item.get("Id")]))
            .first()
        )
    parts = (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            MediaIdentity.jellyfin_id.in_([item_id, item.get("Id")]),
            MediaPart.kind == "movie_file",
            MediaPart.has_file.is_(True),
        )
        .all()
    )
    # A film's copies share Jellyfin's title id; the file name tells them apart.
    name = PurePath(item.get("Path") or "").name
    named = [p for p in parts if p.file_path and PurePath(p.file_path).name == name]
    if named:
        return named[0]
    return parts[0] if len(parts) == 1 else None


def _label(part) -> str:
    from .services import episode_label

    if part.kind == "episode" and part.season_number is not None:
        number = episode_label(part.season_number, part.episode_number or 0)
        return f"{part.media.title} {number}"
    return part.media.title


def _playing(sessions) -> set[tuple[str, str]]:
    return {
        (_norm(s.get("UserId")), _norm((s.get("NowPlayingItem") or {}).get("Id")))
        for s in sessions or []
        if s.get("NowPlayingItem")
    }


def sweep(now=None) -> int:
    """Worker cycle: returns how many were marked watched."""
    from .services import (
        _as_datetime,
        audit,
        client,
        counted_users,
        set_setting,
        setting,
    )

    if not enabled():
        return 0
    now = now or utcnow()
    stored = setting(KEY) or {}
    checked = _as_datetime(stored.get("at"))
    if checked and now - checked < EVERY:
        return 0
    jellyfin = client("jellyfin")
    try:
        users = counted_users(jellyfin.users())
        playing = _playing(jellyfin.sessions())
        stopped = [
            (user, item)
            for user in users
            for item in jellyfin.resume(user["Id"], fields="Path")
        ]
    except IntegrationError:
        return 0
    db.session.rollback()  # asked Jellyfin outside any transaction
    done = 0
    for user, item in stopped:
        if (_norm(user["Id"]), _norm(item.get("Id"))) in playing:
            continue  # still on screen: paused in the credits, maybe
        runtime = (item.get("RunTimeTicks") or 0) / TICKS
        position = (
            (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
        ) / TICKS
        if not runtime or not position:
            continue
        try:
            credits = credits_start(jellyfin.media_segments(item["Id"]), runtime)
        except IntegrationError:
            credits = None
        part = _part_for(item)
        dialogue = None
        if part is not None:
            state = (
                db.session.query(ChapterState).filter_by(media_part_id=part.id).first()
            )
            dialogue = dialogue_ends(part, state, runtime)
        why = finished_reason(
            position, runtime, credits, dialogue, item.get("Type") == "Episode"
        )
        if not why:
            continue
        try:
            jellyfin.mark_played(user["Id"], item["Id"])
        except IntegrationError:
            continue
        done += 1
        left = max(1, round((runtime - position) / 60))
        what = _label(part) if part is not None else item.get("Name")
        if part is not None:
            audit(
                "finished_at_credits",
                part.media_identity_id,
                f"{user.get('Name') or 'Someone'} stopped {what} {why}, "
                f"{left} min from the end; marked watched so it leaves Continue Watching.",
                f"finished-at-credits:{_norm(user['Id'])}:{_norm(item['Id'])}:"
                f"{int(position)}",
                payload={"user": user["Id"], "item": item["Id"], "position": position},
            )
    set_setting(KEY, {"at": now.isoformat()})
    db.session.commit()
    return done
