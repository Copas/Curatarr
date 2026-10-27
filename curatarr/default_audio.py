"""Don't play a commentary or an audio description by default.

Some releases make the director's commentary or the audio description (a
narrator describing the picture, for blind viewers) the default audio track,
so that's what Jellyfin plays. When the default track is one of those, by its
flags or its name, and the file has its own soundtrack in the same language,
that soundtrack becomes the default instead: in place, with mkvpropedit, no
re-encode (Settings → Enhance). Unless the fix is automatic (Settings →
Automatic, off by default), a file found like this is listed on Problems with
Fix and Leave it, and changed only once Fix is pressed.

Every file is looked at once, a few each worker cycle (it means reading each
file's track list), and again when it's replaced. Files being played, and
files in dry-run libraries, wait.
"""

import re
import time

from . import db
from .integrations import IntegrationError
from .models import ChapterState, MediaIdentity, MediaPart, utcnow

RUN_SECONDS = 20
EXTRA_NAME = re.compile(
    r"comment|descri|narrat|visually impaired|\bad\b|\bdvs\b", re.IGNORECASE
)


def enabled() -> bool:
    from .enrichment import options

    try:
        return bool(options()["fix_default_audio"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def _extra(stream) -> bool:
    return bool(stream.get("extra") or EXTRA_NAME.search(stream.get("title") or ""))


def better_default(streams) -> int | None:
    """The track to make the default, or None when the default is fine: the
    file's own soundtrack in the default track's language (most channels),
    when the default is a commentary or an audio description."""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio) < 2:
        return None
    current = next((s for s in audio if s.get("default")), audio[0])
    if not _extra(current):
        return None
    own = [
        s
        for s in audio
        if not _extra(s)
        and (s.get("language") or "") == (current.get("language") or "")
    ]
    if not own:
        return None
    return max(own, key=lambda s: s.get("channels") or 0)["index"]


def _due():
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind.in_(("movie_file", "episode")),
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            MediaIdentity.missing_since.is_(None),
            (ChapterState.id.is_(None))
            | ChapterState.default_audio_size.is_(None)
            | (ChapterState.default_audio_size != MediaPart.size_bytes),
        )
        .order_by(MediaPart.kind.desc(), MediaPart.id)  # films first
    )


def _automatic() -> bool:
    from . import autofix

    return autofix.automatic("default_audio")


def found_rows() -> list:
    """Files found playing an extra first, waiting for Fix (or Leave it), when
    the fix isn't automatic (Settings → Automatic)."""
    rows = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.default_audio.isnot(None),
            MediaIdentity.missing_since.is_(None),
        )
        .all()
    )
    return [
        r
        for r in rows
        if (r.default_audio or {}).get("found")
        and not (r.default_audio or {}).get("dismissed")
    ]


def _mark(state_id, **changes) -> None:
    state = db.session.get(ChapterState, state_id)
    if state is None or not (state.default_audio or {}).get("found"):
        raise ValueError("Nothing waiting for that file")
    state.default_audio = dict(state.default_audio) | changes
    db.session.commit()


def approve(state_id) -> None:
    """Fix it on the next worker cycle."""
    _mark(state_id, approved=True)


def approve_all() -> int:
    rows = found_rows()
    for state in rows:
        state.default_audio = dict(state.default_audio) | {"approved": True}
    db.session.commit()
    return len(rows)


def dismiss(state_id) -> None:
    _mark(state_id, dismissed=True)


def sweep() -> int:
    """Worker cycle: look at a few files; returns how many were fixed."""
    from .compat_audio import _playing
    from .enrichment import (
        _refresh_jellyfin,
        can_write,
        file_exists,
        file_label,
        local_path,
        probe,
        state_for,
    )
    from .filecheck_jobs import _dry_run
    from .mediafile import MediaToolError, set_default_audio
    from .services import audit

    if not enabled():
        return 0
    started, fixed = time.monotonic(), 0
    automatic = _automatic()
    waiting = [
        s.part
        for s in found_rows()
        if automatic or (s.default_audio or {}).get("approved")
    ]
    seen = set()
    for part in waiting[:20] + _due().limit(200).all():
        if time.monotonic() - started > RUN_SECONDS:
            break
        if part.id in seen:
            continue  # approved, and due as well: once is enough
        seen.add(part.id)
        path = local_path(part.file_path)
        if not path or not file_exists(path):
            continue  # unreachable for now
        state = state_for(part)
        try:
            info = probe(path)
        except (MediaToolError, OSError):
            continue
        choice = better_default(info.streams)
        record = {"at": utcnow().isoformat()}
        previous = state.default_audio or {}
        if state.default_audio_size != info.size:
            previous = {}  # a new copy of the file: an earlier Fix or Leave it was for the old one
        if choice is not None:
            writable, reason = can_write(info)
            audio = [s for s in info.streams if s.get("codec_type") == "audio"]
            before = next((s for s in audio if s.get("default")), audio[0])
            if not writable or _dry_run(part.media):
                record["skipped"] = reason or "dry run"
            elif not automatic and not previous.get("approved"):
                # Ask first: listed on Problems with Fix and Leave it.
                after = next(s for s in info.streams if s["index"] == choice)
                record |= {
                    "found": True,
                    "from": before.get("title") or before.get("codec_name"),
                    "to": after.get("title") or after.get("codec_name"),
                    "dismissed": previous.get("dismissed", False),
                }
            elif _playing(part):
                continue  # look again once it's stopped
            else:
                in_sync = (state.chapter_file_size, state.chapter_file_mtime) == (
                    info.size,
                    info.mtime,
                )
                try:
                    set_default_audio(path, info, choice)
                    info = probe(path)
                except (MediaToolError, OSError) as exc:
                    record["error"] = str(exc)[:200]
                else:
                    if in_sync:  # Curatarr's chapters are still its own
                        state.chapter_file_size = info.size
                        state.chapter_file_mtime = info.mtime
                    now_default = next(s for s in info.streams if s["index"] == choice)
                    record |= {
                        "fixed": True,
                        "from": before.get("title") or before.get("codec_name"),
                        "to": now_default.get("title") or now_default.get("codec_name"),
                    }
                    try:
                        _refresh_jellyfin(part, part.media)
                    except IntegrationError:
                        pass
                    audit(
                        "default_audio_fixed",
                        part.media_identity_id,
                        f"{file_label(state)} played “{record['from']}” by default; "
                        "its own soundtrack is the default now.",
                        f"default-audio-extra:{state.id}:{record['at']}",
                        payload={"file": path, "stream": choice},
                    )
                    fixed += 1
        state.default_audio_size = info.size
        if part.size_bytes != info.size and record.get("fixed"):
            part.size_bytes = info.size
        state.default_audio = record
        db.session.commit()
    return fixed
