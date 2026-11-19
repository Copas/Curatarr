"""An episode's English subtitles as a transcript, for finding recaps.

A recap is recognised by lines quoted from earlier episodes, so it needs
those episodes' words. Curatarr transcribes an episode when it makes its
subtitles, so an episode that came with good subtitles (and chapters) has
only its opening transcribed: the next episode's recap, made of its scenes,
then matched nothing (FROM S4E2's recap stopped after 19 s, the clips from
S4E1 unrecognised). Their subtitles say the same words, so they stand in:
a sidecar .srt beside the file, or a text subtitle track inside it (the
plain one before SDH). Read once, then kept with the transcripts.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import throttle
from .chapters import Segment
from .mediafile import tool

ENGLISH = {"en", "eng", "english"}
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
EXTRACT_SECONDS = 180  # reading a track means reading through the whole file
# What SDH subtitles add that nobody says: [door creaks], (sighs), "SAM:".
NOT_SAID = re.compile(r"\[[^\]]*\]|\([^)]*\)|♪")
SPEAKER = re.compile(r"\b[A-Z][A-Z.'-]+(?: [A-Z][A-Z.'-]+)*:\s*")


def _said(text: str) -> str:
    return " ".join(SPEAKER.sub(" ", NOT_SAID.sub(" ", text)).split())


def segments_from_srt(text: str) -> list[Segment]:
    from .subtitle_sync import parse_srt

    found = []
    for line in parse_srt(text):
        words = _said(line.text)
        if words:
            found.append(Segment(line.start, line.end, words))
    return found


def _sidecar(path: Path) -> Path | None:
    stem = path.with_suffix("")
    for suffix in (".en.srt", ".eng.srt", ".english.srt", ".srt"):
        candidate = Path(f"{stem}{suffix}")
        if candidate.is_file():
            return candidate
    return None


def _embedded_track(path: Path) -> int | None:
    """The stream index of the plain English text subtitle track (SDH or
    forced ones only when there's nothing else)."""
    probe = tool("ffprobe")
    if not probe:
        return None
    try:
        throttle.wait_while_paused()
        result = throttle.run(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
                "-of",
                "json",
                str(path),
            ],
            timeout=60,
        )
        if result.returncode != 0:
            return None
        streams = json.loads(result.stdout).get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    ranked = []
    for stream in streams:
        tags = stream.get("tags") or {}
        if (tags.get("language") or "").lower() not in ENGLISH:
            continue
        if stream.get("codec_name") not in TEXT_CODECS:
            continue
        disposition = stream.get("disposition") or {}
        title = (tags.get("title") or "").lower()
        forced = disposition.get("forced") or "forced" in title
        sdh = disposition.get("hearing_impaired") or "sdh" in title
        ranked.append((bool(forced), bool(sdh), stream["index"]))
    ranked.sort()
    if not ranked or ranked[0][0]:
        return None  # forced subtitles carry only the foreign lines
    return ranked[0][2]


def _extract(path: Path, index: int) -> str | None:
    ffmpeg = tool("ffmpeg")
    if not ffmpeg:
        return None
    # Reading a track reads the whole file from the share: like the chapter
    # jobs' tools it waits, and is suspended, while someone is watching
    # (throttle), or it starves their stream.
    try:
        throttle.wait_while_paused()
        result = throttle.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                f"0:{index}",
                "-f",
                "srt",
                "-",
            ],
            timeout=EXTRACT_SECONDS,
        )
        if result.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return result.stdout


def transcript(path: str | None, cache: Path) -> list[Segment] | None:
    """The episode's English subtitles as segments, or None (no file, no
    English text subtitles, or nothing in them)."""
    if not path:
        return None
    media = Path(path)
    try:
        stat = media.stat()
    except OSError:
        return None
    key = hashlib.sha256(
        f"{path}|{stat.st_size}|{int(stat.st_mtime)}".encode()
    ).hexdigest()
    kept = cache / f"subs-{key[:32]}.json"
    if kept.exists():
        try:
            rows = json.loads(kept.read_text(encoding="utf-8"))
            return [Segment(*row) for row in rows] or None
        except (OSError, ValueError, TypeError):
            pass
    sidecar = _sidecar(media)
    if sidecar:
        try:
            text = sidecar.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = None
    else:
        index = _embedded_track(media)
        text = _extract(media, index) if index is not None else None
    segments = segments_from_srt(text) if text else []
    try:
        cache.mkdir(parents=True, exist_ok=True)
        tmp = kept.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps([[s.start, s.end, s.text] for s in segments]), encoding="utf-8"
        )
        tmp.replace(kept)  # none found is kept too: not looked for again
    except OSError:
        pass
    return segments or None
