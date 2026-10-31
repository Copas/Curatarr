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


# Subtitles stored as pictures: TV apps can't draw them, so Jellyfin burns
# them into the video.
PICTURE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
PICTURE_SUFFIXES = {".sup", ".idx", ".sub"}


def embedded_english(streams: list[dict], text_only=False) -> bool:
    for stream in streams:
        if stream.get("codec_type") != "subtitle" or stream.get("forced"):
            continue
        if text_only and stream.get("codec_name") in PICTURE_CODECS:
            continue
        language = (stream.get("language") or "").lower()
        if language in ENGLISH or language in UNTAGGED:
            return True
    return False


def sidecar_english(video_path: str, text_only=False) -> list[str]:
    """Subtitle files beside the video that are English, or untagged and not
    plainly another language (with ``text_only``, not pictures: VobSub
    .idx/.sub, Blu-ray .sup).

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
        if text_only and _picture_file(candidate):
            continue
        if name.endswith(GENERATED_SUFFIX):
            continue
        tags = name[len(video.stem) + 1 : -len(candidate.suffix)].lower().split(".")
        tags = [tag for tag in tags if tag]
        if "forced" in tags:
            continue
        if any(tag in ENGLISH for tag in tags):
            found.append(name)
        elif not tags and written_in_english(candidate) is not False:
            # Untagged: English unless its words say otherwise.
            found.append(name)
    return found


# The commonest English words. In English subtitles they make up a third or
# more of all words; in other languages, even ones written in the same
# letters, a few in a hundred (shared words like "a", "in", "no" included).
COMMON_ENGLISH = frozenset(
    [
        "the",
        "you",
        "i",
        "to",
        "a",
        "and",
        "it",
        "is",
        "that",
        "of",
        "what",
        "in",
        "me",
        "this",
        "we",
        "he",
        "your",
        "on",
        "my",
        "no",
        "be",
        "for",
        "have",
        "not",
        "do",
        "are",
        "was",
        "don't",
        "it's",
        "i'm",
        "know",
        "just",
        "with",
        "so",
        "all",
        "can",
    ]
)
ENGLISH_SHARE = 0.15
MIN_WORDS = 50
SAMPLE_BYTES = 65536


def decode(raw: bytes) -> tuple[str, str]:
    """A subtitle file's text and the encoding to write it back in: UTF-8
    (with or without its marker), UTF-16 (a zero byte beside most letters;
    usually marked, sometimes not), else Western European (Windows-1252)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    sample = raw[:4096]
    if sample and sample.count(0) * 4 > len(sample):
        encoding = (
            "utf-16-le" if sample[1::2].count(0) > sample[::2].count(0) else "utf-16-be"
        )
        return raw.decode(encoding, errors="replace"), encoding
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def read_text(path) -> tuple[str, str]:
    with open(path, "rb") as handle:
        return decode(handle.read())


def written_in_english(path) -> bool | None:
    """Whether a text subtitle file reads as English, from its words. None
    when it can't be told: unreadable, or too few words."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(SAMPLE_BYTES)
    except OSError:
        return None
    if raw[:2] not in (b"\xff\xfe", b"\xfe\xff") and len(raw) % 2 and raw.count(0):
        raw = raw[:-1]  # cut mid-letter in UTF-16
    lines = decode(raw)[0].splitlines()
    if any(line.startswith("Dialogue:") for line in lines):
        # SubStation (.ass/.ssa): only what's said, not the styling around it.
        lines = [
            line.split(",", 9)[-1] for line in lines if line.startswith("Dialogue:")
        ]
    text = re.sub(r"<[^>]*>|\{[^}]*\}|\\N", " ", "\n".join(lines)).lower()
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text)
    if len(words) < MIN_WORDS:
        return None
    return sum(word in COMMON_ENGLISH for word in words) >= ENGLISH_SHARE * len(words)


def _picture_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix == ".sub":  # VobSub pictures beside an .idx; else MicroDVD text
        return path.with_suffix(".idx").exists()
    return suffix in PICTURE_SUFFIXES


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
