"""Generated English subtitles from the Smart Chapters transcript.

Pure helpers: whether a file already has English subtitles, turning transcript
segments into readable cues, and the SRT text. Writing the file and recording
it happen in enrichment.py.

A generated file is named ``<video name>.en.Curatarr.srt`` beside the video,
so Jellyfin lists it as an English external subtitle and nobody mistakes it
for a downloaded one. Curatarr never writes over a file it did not create.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .chapters import Segment

ENGLISH = {"en", "eng", "english"}
# Tracks with no language tag are usually the film's own language; counting
# them as English avoids adding a second English track beside a real one.
UNTAGGED = {"", "und", "unknown", None}
TEXT_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".smi"}
GENERATED_SUFFIX = ".en.Curatarr.srt"

MAX_LINE = 42
MAX_LINES = 2
MAX_CUE_SECONDS = 7.0
MIN_CUE_SECONDS = 1.0
GAP_SECONDS = 0.05


def generated_path(video_path: str) -> Path:
    path = Path(video_path)
    return path.with_name(path.stem + GENERATED_SUFFIX)


def embedded_english(streams: list[dict]) -> bool:
    for stream in streams:
        if stream.get("codec_type") != "subtitle" or stream.get("forced"):
            continue
        language = (stream.get("language") or "").lower()
        if language in ENGLISH or language in UNTAGGED:
            return True
    return False


def sidecar_english(video_path: str) -> list[str]:
    """Subtitle files beside the video that are English or untagged.

    ``Show.S01E01.en.srt``, ``Show.S01E01.eng.forced.srt`` (forced excluded),
    ``Show.S01E01.srt``. Curatarr's own generated file is not counted.
    """
    video = Path(video_path)
    found = []
    try:
        siblings = list(video.parent.iterdir())
    except OSError:
        return found
    for candidate in siblings:
        name = candidate.name
        if not name.startswith(video.stem + ".") or candidate == video:
            continue
        if candidate.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if name.endswith(GENERATED_SUFFIX):
            continue
        tags = name[len(video.stem) + 1 : -len(candidate.suffix)].lower().split(".")
        tags = [tag for tag in tags if tag]
        if "forced" in tags:
            continue
        if not tags or any(tag in ENGLISH for tag in tags):
            found.append(name)
    return found


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]


def _wrap(words: list[str]) -> list[str]:
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > MAX_LINE:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _balance(text: str) -> list[str]:
    """Two lines of similar length read better than a long and a short one."""
    words = text.split()
    if len(text) <= MAX_LINE:
        return [text]
    best = None
    for cut in range(1, len(words)):
        first, second = " ".join(words[:cut]), " ".join(words[cut:])
        if len(first) <= MAX_LINE and len(second) <= MAX_LINE:
            score = abs(len(first) - len(second))
            if best is None or score < best[0]:
                best = (score, [first, second])
    return best[1] if best else _wrap(words)[:MAX_LINES]


def _chunks(text: str) -> list[str]:
    """Split one segment's text into pieces that each fit one cue."""
    words = text.split()
    chunks, current = [], []
    for word in words:
        trial = current + [word]
        if len(_wrap(trial)) > MAX_LINES:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current = trial
        # Prefer to end a cue at the end of a sentence once it is reasonably full.
        if re.search(r"[.?!]$", word) and len(" ".join(current)) > MAX_LINE:
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    return chunks


def cues_from(segments: list[Segment]) -> list[Cue]:
    """Readable cues: at most two lines of ~42 characters, at most 7 seconds."""
    cues: list[Cue] = []
    for segment in segments:
        text = " ".join(segment.text.split())
        if not text or segment.end <= segment.start:
            continue
        pieces = _chunks(text)
        total = sum(len(p) for p in pieces) or 1
        start = segment.start
        span = segment.end - segment.start
        for piece in pieces:
            length = span * len(piece) / total
            end = start + length
            cue_start = start
            # A long piece gets split further in time, not in text.
            while end - cue_start > MAX_CUE_SECONDS:
                cues.append(
                    Cue(cue_start, cue_start + MAX_CUE_SECONDS, _balance(piece))
                )
                cue_start += MAX_CUE_SECONDS
            cues.append(Cue(cue_start, end, _balance(piece)))
            start = end
    # No overlaps, and nothing flashes by too fast when there is room.
    for index, cue in enumerate(cues):
        following = cues[index + 1].start if index + 1 < len(cues) else None
        if cue.end - cue.start < MIN_CUE_SECONDS:
            cue.end = cue.start + MIN_CUE_SECONDS
        if following is not None and cue.end > following - GAP_SECONDS:
            # Never past the next cue, even when that leaves this one short.
            cue.end = min(max(cue.start + 0.3, following - GAP_SECONDS), following)
    return [cue for cue in cues if cue.end > cue.start]


def _srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def srt_text(cues: list[Cue]) -> str:
    blocks = []
    for number, cue in enumerate(cues, start=1):
        blocks.append(
            f"{number}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n"
            + "\n".join(cue.lines)
        )
    return "\n\n".join(blocks) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
