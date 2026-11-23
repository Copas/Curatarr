"""Playback trouble: files a device keeps giving up on early.

When a TV or streaming stick can't play a file properly (the sound keeps
cutting out, the picture stalls), people try it once or twice and give up a
few minutes in. Nobody may say. Curatarr notices from Jellyfin's sessions,
which it reads every worker cycle anyway: each go at playing a file on one
device is a try (PlaybackTry), with how far it got and how it was played.

A file is in trouble on a device when it was tried there at least twice
within a day, every try stopped in the first few minutes, and nothing since
got further. What's likely wrong comes from how Jellyfin played it (direct
play, direct stream or transcode, and why) and the file's own formats.

When the audio is one TVs may not play as it is (not AC3, E-AC3 or AAC) and
TV-friendly audio is on, the file is given an AC3 track first in line.
Everything else is listed on Enhance → Problems, with a note when several
files with the same audio or video share the trouble, so a format nobody
expected to be hard stands out. Someone trying a show and deciding against
it looks the same: that's why only the audio is acted on, and Keep dismisses.
"""

from datetime import timedelta

from flask import current_app
from sqlalchemy import or_

from . import db
from .models import (
    MediaIdentity,
    MediaPart,
    PlaybackTrouble,
    PlaybackTry,
    utcnow,
)

TICKS = 10_000_000
GONE_AFTER = timedelta(minutes=2)  # not seen in the sessions since: it ended
EARLY = 5 * 60  # seconds: a try that stopped before this gave up early
TRIES = 2  # this many early tries on one device...
WITHIN = timedelta(days=1)  # ...this close together
KEEP_TRIES = timedelta(days=30)
PATTERN = 3  # files sharing a format before it's called out
EASY_AUDIO = {"ac3", "eac3", "aac"}


def bare(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


def _default_stream(item, kind):
    streams = [s for s in item.get("MediaStreams") or [] if s.get("Type") == kind]
    chosen = next((s for s in streams if s.get("IsDefault")), None)
    return chosen or (streams[0] if streams else {})


def _part_for(item_id, raw=None):
    ids = {item_id, raw or item_id}
    part = db.session.query(MediaPart).filter(MediaPart.jellyfin_id.in_(ids)).first()
    if part is None:
        part = (
            db.session.query(MediaPart)
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(
                MediaIdentity.jellyfin_id.in_(ids),
                MediaPart.kind == "movie_file",
                MediaPart.has_file.is_(True),
            )
            .first()
        )
    return part


def record(sessions, now=None) -> int:
    """Note each episode or film being played, and end the tries no longer
    in the sessions; returns how many are playing."""
    now = now or utcnow()
    seen = set()
    for session in sessions or []:
        item = session.get("NowPlayingItem") or {}
        if item.get("Type") not in ("Episode", "Movie"):
            continue
        item_id = bare(item.get("Id"))
        session_id = str(session.get("Id") or "")
        if not item_id or not session_id:
            continue
        state = session.get("PlayState") or {}
        position = (state.get("PositionTicks") or 0) / TICKS
        try_ = (
            db.session.query(PlaybackTry)
            .filter_by(session_id=session_id, item_id=item_id, ended=False)
            .first()
        )
        if try_ is None:
            transcoding = session.get("TranscodingInfo") or {}
            part = _part_for(item_id, str(item.get("Id")))
            try_ = PlaybackTry(
                session_id=session_id,
                item_id=item_id,
                media_part_id=part.id if part else None,
                user_id=bare(session.get("UserId")) or None,
                device=session.get("DeviceName"),
                client=session.get("Client"),
                runtime=(item.get("RunTimeTicks") or 0) / TICKS or None,
                video_codec=_default_stream(item, "Video").get("Codec"),
                audio_codec=_default_stream(item, "Audio").get("Codec"),
                transcode_reasons=transcoding.get("TranscodeReasons") or None,
                started_at=now,
                last_seen_at=now,
                furthest=position,
            )
            db.session.add(try_)
        try_.play_method = state.get("PlayMethod") or try_.play_method
        reasons = (session.get("TranscodingInfo") or {}).get("TranscodeReasons")
        if reasons:
            try_.transcode_reasons = reasons
        try_.furthest = max(try_.furthest or 0, position)
        try_.last_seen_at = now
        db.session.flush()
        seen.add(try_.id)
    for try_ in db.session.query(PlaybackTry).filter(
        PlaybackTry.ended.is_(False), PlaybackTry.last_seen_at < now - GONE_AFTER
    ):
        try_.ended = True
    db.session.query(PlaybackTry).filter(
        PlaybackTry.started_at < now - KEEP_TRIES
    ).delete(synchronize_session=False)
    db.session.commit()
    return len(seen)


def _early(try_) -> bool:
    limit = EARLY
    if try_.runtime:
        limit = min(limit, try_.runtime / 2)
    return (try_.furthest or 0) < limit


def _diagnosis(tries) -> dict:
    """What the tries say about why: how it was played, and what the file
    has that the device may not."""
    last = tries[-1]
    method = last.play_method or "unknown"
    reasons = sorted({r for t in tries for r in (t.transcode_reasons or [])})
    audio = (last.audio_codec or "").lower()
    suspect = None
    if audio and audio not in EASY_AUDIO and "Audio" not in " ".join(reasons):
        # Sent as it is (or the video converted and the audio passed through).
        suspect = "audio"
    elif reasons:  # converted while it played: video, bitrate or container
        suspect = "conversion"
    words = {
        "audio": f"{audio.upper()} audio sent to the device as it is",
        "conversion": "Jellyfin converting it while it plays"
        + (f" ({', '.join(reasons)})" if reasons else ""),
        None: "nothing in how it was played stands out",
    }[suspect]
    return {
        "device": last.device,
        "client": last.client,
        "method": method,
        "reasons": reasons,
        "video": last.video_codec,
        "audio": last.audio_codec,
        "tries": len(tries),
        "furthest": max(t.furthest or 0 for t in tries),
        "suspect": suspect,
        "words": words,
    }


def find(now=None) -> list:
    """New troubles from the tries: (item, device) pairs given up on early
    TRIES times within WITHIN, with nothing since getting further. Recorded
    once each; returns the new ones."""
    now = now or utcnow()
    tries = (
        db.session.query(PlaybackTry)
        .filter(PlaybackTry.started_at >= now - WITHIN * 3)
        .order_by(PlaybackTry.started_at)
        .all()
    )
    by_pair = {}
    for try_ in tries:
        by_pair.setdefault((try_.item_id, try_.device or ""), []).append(try_)
    known = {
        (t.item_id, t.device)
        for t in db.session.query(PlaybackTrouble.item_id, PlaybackTrouble.device)
    }
    found = []
    for (item_id, device), pair in by_pair.items():
        if (item_id, device) in known or not device:
            continue
        if any(not t.ended for t in pair):
            continue  # still playing: wait and see
        if not _early(pair[-1]):
            continue  # the last go got going: whatever it was, it's past
        early = [t for t in pair if _early(t)]
        recent = [t for t in early if t.started_at >= pair[-1].started_at - WITHIN]
        if len(recent) < TRIES:
            continue
        trouble = PlaybackTrouble(
            item_id=item_id,
            device=device,
            media_part_id=next(
                (t.media_part_id for t in pair if t.media_part_id), None
            ),
            found_at=now,
            last_try_at=pair[-1].started_at,
            details=_diagnosis(recent),
        )
        db.session.add(trouble)
        found.append(trouble)
    db.session.commit()
    return found


def act(troubles) -> int:
    """Put the files whose audio is the likely cause first in line for a
    TV-friendly track; returns how many were queued."""
    from . import compat_audio

    if not compat_audio.tv_enabled():
        return 0
    queued = 0
    for trouble in troubles:
        if trouble.details.get("suspect") != "audio" or not trouble.media_part_id:
            continue
        trouble.state = "queued"
        queued += 1
    db.session.commit()
    return queued


def queued_part_ids() -> set:
    """Files TV-friendly audio should do first, whatever their audio is
    (compat_audio)."""
    return {
        part_id
        for (part_id,) in db.session.query(PlaybackTrouble.media_part_id).filter(
            PlaybackTrouble.state == "queued", PlaybackTrouble.media_part_id.isnot(None)
        )
    }


def done(part_id, added: bool) -> None:
    """TV-friendly audio has looked at a queued file: listed again if it
    couldn't add a track, so someone sees it."""
    for trouble in db.session.query(PlaybackTrouble).filter_by(
        media_part_id=part_id, state="queued"
    ):
        trouble.state = "fixed" if added else "open"
        if not added:
            trouble.details = dict(trouble.details) | {"not_fixed": True}


def sweep(sessions, now=None) -> None:
    """Each worker cycle: record what's playing, find and act on trouble."""
    record(sessions, now)
    found = find(now)
    log_found(found)
    act(found)


def _label(part):
    if part is None:
        return None
    media = part.media
    if part.kind == "episode":
        return f"{media.title} · S{part.season_number or 0:02}E{part.episode_number or 0:02}"
    return media.title


def rows() -> list:
    """The open troubles for Problems, newest first."""
    out = []
    for trouble in (
        db.session.query(PlaybackTrouble)
        .filter(or_(PlaybackTrouble.state == "open", PlaybackTrouble.state == "queued"))
        .order_by(PlaybackTrouble.last_try_at.desc())
    ):
        part = (
            db.session.get(MediaPart, trouble.media_part_id)
            if trouble.media_part_id
            else None
        )
        out.append(
            {
                "id": trouble.id,
                "state": trouble.state,
                "media": part.media if part else None,
                "part_id": part.id if part else None,
                "label": _label(part) or trouble.details.get("name") or trouble.item_id,
                "last_try_at": trouble.last_try_at,
                **trouble.details,
            }
        )
    return out


def patterns() -> list:
    """Formats several troubled files share on the same kind of player:
    [{"what", "client", "files"}], most files first."""
    counts = {}
    for (details,) in db.session.query(PlaybackTrouble.details).filter(
        PlaybackTrouble.state != "kept"
    ):
        client = details.get("client") or "a player"
        for what in (
            f"{(details.get('audio') or '').upper()} audio"
            if details.get("audio")
            else None,
            f"{(details.get('video') or '').upper()} video"
            if details.get("video")
            else None,
        ):
            if what:
                counts[(what, client)] = counts.get((what, client), 0) + 1
    return [
        {"what": what, "client": client, "files": n}
        for (what, client), n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= PATTERN
    ]


def count() -> int:
    return (
        db.session.query(PlaybackTrouble)
        .filter(PlaybackTrouble.state == "open")
        .count()
    )


def keep(trouble_id) -> None:
    """Dismiss: it was someone deciding against it, or it's been seen to."""
    trouble = db.session.get(PlaybackTrouble, trouble_id)
    if trouble is None:
        raise ValueError("That playback trouble is gone")
    trouble.state = "kept"
    db.session.commit()


def log_found(found) -> None:
    for trouble in found:
        current_app.logger.info(
            "Playback trouble on %s: %s", trouble.device, trouble.details.get("words")
        )
