"""Don't play a commentary or an audio description by default.

Some releases make the director's commentary or the audio description (a
narrator describing the picture, for blind viewers) the default audio track,
so that's what Jellyfin plays. When the default track is one of those, by its
flags or its name, and the file has its own soundtrack in the same language,
that soundtrack becomes the default instead: in place, with mkvpropedit, no
re-encode (Settings → Enhance, on by default).

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
    for part in _due().limit(200).all():
        if time.monotonic() - started > RUN_SECONDS:
            break
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
        if choice is not None:
            writable, reason = can_write(info)
            if not writable or _dry_run(part.media):
                record["skipped"] = reason or "dry run"
            elif _playing(part):
                continue  # look again once it's stopped
            else:
                audio = [s for s in info.streams if s.get("codec_type") == "audio"]
                before = next((s for s in audio if s.get("default")), audio[0])
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
