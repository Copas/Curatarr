"""Whether a subtitle file beside a video lines up with what's said.

Downloaded subtitles are often timed for another release of the same film:
a different opening shifts every line by a few seconds, and a different frame
rate (24 against 23.976, 25 against 24) makes them drift further out as the
film goes on. Jellyfin lists such a file like any other, so nobody notices
until the lines turn up long after they're spoken.

A few stretches of the film are transcribed and each spoken line is matched
to the subtitle line with the same words. How far apart they are, at points
spread through the film, gives a straight line: a fixed delay plus a steady
drift. A file that fits such a line is re-timed; one that fits none (another
cut of the film, or another film) can't be fixed by shifting it.

Pure helpers: reading and re-timing SRT text, measuring and judging. The
transcription and the files happen in enrichment.py.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from statistics import median

from .chapters import Segment

# Where to listen, as fractions of the running time, and for how long.
WINDOWS = (0.12, 0.37, 0.62, 0.87)
WINDOW_SECONDS = 150
# How far from the spoken line its subtitle may be and still be found.
SEARCH_SECONDS = 120
MIN_CHARACTERS = 12  # shorter lines ("Yeah.") match too many others
MIN_MATCH = 0.6
MIN_POINTS = 8
MIN_WINDOW_POINTS = 3  # a stretch counts with at least this many lines found
# Enough lines to say the file can't be fixed by shifting it, rather than
# only that there isn't enough speech to tell.
SURE_POINTS = 20
# A file this close everywhere is in sync: a subtitle usually appears a
# moment before the voice, and speech recognition times lines to within a
# second or so (files known to be right measure up to ~1.2 s).
IN_SYNC_SECONDS = 1.5
# Uneven but never further out than this anywhere it was heard: not worth
# replacing someone's subtitles over (they read fine).
CLOSE_SECONDS = 3.0
# Lines further from the fitted line than this don't count, and a fit
# where the rest scatter more than SPREAD_SECONDS isn't a straight shift.
OUTLIER_SECONDS = 2.0
SPREAD_SECONDS = 1.0
MIN_INLIERS = 0.6
# Too few of plenty of spoken lines found at all: not this film's words.
PLENTY_SPOKEN = 30
MIN_FOUND = 0.15
# Far fewer subtitle lines than spoken ones: subtitles only for signs or the
# parts in another language, which aren't meant to follow the speech.
SPARSE = 0.4
# Drift smaller than this over the whole file is measuring noise.
MIN_DRIFT_SECONDS = 1.0
# Frame-rate conversions, snapped to when the measured drift is close (within
# a share of how far each is from 1, at most SNAP); anything outside RATES
# isn't a frame-rate difference at all.
KNOWN_RATES = (24 / 23.976, 25 / 24, 25 / 23.976)
SNAP = 0.003
SNAP_SHARE = 0.4
RATES = (0.95, 1.06)
# Each stretch listened to must sit on the line: a film cut differently is
# right in some places and out in others, which one line can't follow.
STRETCH_SECONDS = 0.75

# Bumped when the rules change in a way that should revisit earlier verdicts
# (see outside_subtitles.revisit).
RULES = 2

TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
ARROW = re.compile(
    rf"^[ \t]*{TIME.pattern}[ \t]*-->[ \t]*{TIME.pattern}(.*)$", re.MULTILINE
)


@dataclass
class Line:
    start: float
    end: float
    text: str


@dataclass
class Result:
    verdict: str  # in_sync, shifted, mismatch, unknown
    delay: float = 0.0  # seconds the subtitles run late at the very start
    rate: float = 1.0  # subtitle time per second of the film
    points: int = 0
    spoken: int = 0
    spread: float | None = None
    worst: float | None = None  # the furthest out any line is, in seconds
    reason: str = ""
    offsets: list = field(default_factory=list)
    stretches: list = field(default_factory=list)  # each one's distance off the line

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "delay": round(self.delay, 3),
            "rate": round(self.rate, 6),
            "points": self.points,
            "spoken": self.spoken,
            "spread": None if self.spread is None else round(self.spread, 2),
            "worst": None if self.worst is None else round(self.worst, 1),
            "reason": self.reason,
            "stretches": [round(off, 2) for off in self.stretches],
        }


def _seconds(match, first=1) -> float:
    hours, minutes, secs, millis = (int(match.group(first + i)) for i in range(4))
    return hours * 3600 + minutes * 60 + secs + int(f"{millis:<03}"[:3]) / 1000


def parse_srt(text: str) -> list[Line]:
    lines = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        rows = block.strip().split("\n")
        for index, row in enumerate(rows):
            match = ARROW.match(row)
            if match:
                words = re.sub(r"<[^>]+>|\{[^}]*\}", "", " ".join(rows[index + 1 :]))
                lines.append(Line(_seconds(match), _seconds(match, 5), words.strip()))
                break
    return lines


def windows(duration: float) -> list[tuple[float, float]]:
    """The stretches to transcribe, (start, end) seconds into the file."""
    found = []
    for fraction in WINDOWS:
        start = max(0.0, min(duration * fraction, duration - WINDOW_SECONDS))
        found.append((round(start), round(min(duration, start + WINDOW_SECONDS))))
    return sorted(set(found))


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())


def measure(lines: list[Line], segments: list[Segment]) -> tuple[list, int]:
    """(film time, how late its subtitle is) for each spoken line found in the
    subtitles, and how many spoken lines were long enough to look for."""
    texts = [(line.start, _plain(line.text)) for line in lines]
    points, spoken = [], 0
    for segment in segments:
        said = _plain(segment.text)
        if len(said) < MIN_CHARACTERS:
            continue
        spoken += 1
        best, at = 0.0, None
        for start, text in texts:
            if abs(start - segment.start) > SEARCH_SECONDS or not text:
                continue
            matcher = SequenceMatcher(None, said, text, autojunk=False)
            if matcher.real_quick_ratio() < MIN_MATCH or matcher.quick_ratio() < best:
                continue
            ratio = matcher.ratio()
            if ratio > best:
                best, at = ratio, start
        if best >= MIN_MATCH:
            points.append((segment.start, at - segment.start))
    return points, spoken


def _line(points):
    """Theil–Sen: the median slope between pairs, then the median intercept.
    Mismatched lines don't pull it the way they would a least-squares line."""
    slopes = [
        (b[1] - a[1]) / (b[0] - a[0])
        for i, a in enumerate(points)
        for b in points[i + 1 :]
        if b[0] - a[0] > 60
    ]
    slope = median(slopes) if slopes else 0.0
    return median(o - slope * t for t, o in points), slope


def _snap(rate: float) -> float:
    known = [r for k in KNOWN_RATES for r in (k, 1 / k)]
    nearest = min(known, key=lambda k: abs(rate - k))
    if abs(rate - nearest) <= min(SNAP, SNAP_SHARE * abs(nearest - 1)):
        return nearest
    return rate


def judge(lines: list[Line], segments: list[Segment], duration: float) -> Result:
    points, spoken = measure(lines, segments)
    result = Result("unknown", points=len(points), spoken=spoken, offsets=points)
    shown = sum(
        1
        for line in lines
        for start, end in windows(duration)
        if start <= line.start < end
    )
    if shown < spoken * SPARSE:
        result.reason = (
            f"The subtitles have few lines ({shown} where {spoken} are spoken): "
            "perhaps only for signs or parts in another language"
        )
        return result
    stretches = [
        [(t, o) for t, o in points if start <= t < end]
        for start, end in windows(duration)
    ]
    stretches = [found for found in stretches if len(found) >= MIN_WINDOW_POINTS]
    if len(points) < MIN_POINTS or len(stretches) < 2:
        if spoken >= PLENTY_SPOKEN and len(points) < spoken * MIN_FOUND:
            result.verdict = "mismatch"
            result.reason = (
                f"Only {len(points)} of {spoken} spoken lines are in the subtitles"
                if points
                else f"None of {spoken} spoken lines are in the subtitles"
            )
        else:
            result.reason = f"Too little speech to compare ({len(points)} lines)"
        return result
    delay, slope = _line(points)
    if abs(slope) * duration < MIN_DRIFT_SECONDS:
        rate = 1.0
    else:
        rate = _snap(1 + slope)
    if rate != 1 + slope:  # a known rate: only the delay is left to find
        delay = median(o - (rate - 1) * t for t, o in points)
    inliers = [
        abs(o - delay - (rate - 1) * t)
        for t, o in points
        if abs(o - delay - (rate - 1) * t) <= OUTLIER_SECONDS
    ]
    result.delay, result.rate = delay, rate
    result.spread = median(inliers) if inliers else None
    result.worst = max(abs(delay), abs(delay + (rate - 1) * duration))
    # Each stretch's distance off the line, from its lines that fit at all; a
    # stretch none of whose lines fit is as far off as can be.
    result.stretches = []
    for found in stretches:
        near = [
            o - delay - (rate - 1) * t
            for t, o in found
            if abs(o - delay - (rate - 1) * t) <= OUTLIER_SECONDS
        ]
        result.stretches.append(median(near) if near else OUTLIER_SECONDS)
    # Close enough everywhere it was heard is in sync, whatever the line says.
    heard = [median(o for _, o in found) for found in stretches]
    if all(abs(off) <= IN_SYNC_SECONDS for off in heard):
        result.verdict = "in_sync"
        return result
    straight = (
        RATES[0] < rate < RATES[1]
        and len(inliers) >= MIN_INLIERS * len(points)
        and result.spread <= SPREAD_SECONDS
        and all(abs(off) <= STRETCH_SECONDS for off in result.stretches)
    )
    if not straight and all(abs(off) <= CLOSE_SECONDS for off in heard):
        result.verdict = "in_sync"
        result.reason = (
            f"Up to {max(abs(off) for off in heard):.1f} s out in places: "
            "close enough to leave as they are"
        )
    elif not straight:
        if len(points) >= SURE_POINTS:
            result.verdict = "mismatch"
            result.reason = (
                "The lines are out by different amounts in different places, "
                "so shifting them can't fix it"
            )
        else:
            result.reason = f"Too little speech to tell ({len(points)} lines)"
    elif result.worst <= IN_SYNC_SECONDS:
        result.verdict = "in_sync"
    else:
        result.verdict = "shifted"
        result.reason = describe(result)
    return result


def describe(result: Result) -> str:
    """'11.0 s late at the start, 16.9 s by the end' and the like."""
    late = "late" if result.delay >= 0 else "early"
    text = f"{abs(result.delay):.1f} s {late} at the start"
    if abs(result.rate - 1) > 1e-6:
        text += f", drifting {abs(result.rate - 1) * 3600:.1f} s an hour"
    return text


def retime(text: str, delay: float, rate: float) -> str:
    """SRT text with every time moved to the film's: what was shown at
    ``delay + rate * t`` is shown at ``t``. Nothing else changes."""

    def fixed(match, first):
        seconds = (_seconds(match, first) - delay) / rate
        millis = max(0, round(seconds * 1000))
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        secs, millis = divmod(millis, 1000)
        return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

    def line(match):
        return f"{fixed(match, 1)} --> {fixed(match, 5)}{match.group(9)}"

    return ARROW.sub(line, text)
