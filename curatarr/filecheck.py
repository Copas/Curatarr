"""Problems with a file that someone would otherwise find halfway through it.

Burned-in subtitles
    Subtitles drawn into the picture can't be turned off. They show up as a
    line or two of bright, dark-outlined text, centred near the bottom of the
    frame, while people are talking. With a transcript, Curatarr looks at frames
    from moments of speech and of silence; subtitles are on most of the first and
    few of the second. On-screen text that isn't subtitles (a title card, a
    sign, a caption in a corner) is too tall, off-centre or too rare to count.
    Without a transcript, frames are taken at even intervals and the bar is
    higher.

Broken files
    A file shorter than Radarr or Sonarr says the film or episode runs, a
    picture that stops before the file says it ends (an incomplete download),
    a soundtrack in which nobody speaks for the last part of a film, and
    decoding errors at points through the file.

Only frames and a few seconds of the file are read; nothing is re-encoded.
"""

import io
import re
import subprocess
from itertools import pairwise

from PIL import Image, ImageChops, ImageFilter

from .mediafile import required_tool

# A frame looks subtitled when this share of the bottom band is bright text
# with a dark outline, in a line no taller than LINE_HEIGHT of the band and
# centred within CENTRE of the middle.
TEXT_SCORE = 0.0025
LINE_HEIGHT = 0.26
CENTRE = 0.2
SPEECH_SAMPLES = 14
EVEN_SAMPLES = 24
SKIP_START, SKIP_END = 300, 420  # opening titles and end credits carry text
BURNED_SHARE, CLEAN_SHARE = 0.5, 0.25  # of speech frames with subtitles
EVEN_BURNED_SHARE, EVEN_CLEAN_SHARE = 0.4, 0.2  # without a transcript

SHORT_SHARE = 0.75  # of the listed runtime
SILENT_END_SHARE = 0.6  # last speech before this share of a film's length
DECODE_POINTS = 6
TRUNCATED_AT = (0.8, 0.9)  # no picture at these shares of the length
# Errors from a decoder, not notes about the container (an unused chapter
# track, say), which ffmpeg also prints at error level.
DECODER_ERROR = re.compile(
    r"^\[(h264|hevc|av1|vp9|mpeg4|mpeg2video|vc1|aac|ac3|eac3|dca|truehd|opus|mp3|flac)[ @]",
    re.MULTILINE,
)


def grab(path: str, seconds: float, width: int = 640) -> Image.Image | None:
    """One greyscale frame at ``seconds``, or None when there's no picture."""
    try:
        result = subprocess.run(
            [
                required_tool("ffmpeg"),
                "-v",
                "error",
                "-ss",
                f"{max(seconds, 0):.2f}",
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:-2,format=gray",
                "-f",
                "image2pipe",
                "-vcodec",
                "pgm",
                "-",
            ],
            capture_output=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if not result.stdout:
        return None
    try:
        return Image.open(io.BytesIO(result.stdout))
    except OSError:
        return None


def text_line(image: Image.Image) -> tuple[float, float, float]:
    """(share, line height, centre offset) of outlined text near the bottom.

    Share: of the band's pixels that are bright with a dark pixel close by.
    Line height: the fewest rows holding 85% of them, as a share of the band.
    Centre offset: their horizontal centre's distance from the middle (0-1).
    """
    width, height = image.size
    band = image.crop((int(width * 0.1), int(height * 0.62), int(width * 0.9), height))
    bright = band.point(lambda v: 255 if v >= 190 else 0)
    dark = band.point(lambda v: 255 if v <= 60 else 0).filter(ImageFilter.MaxFilter(5))
    text = ImageChops.multiply(bright, dark)
    w, h = text.size
    pixels = text.load()
    rows, cols = [0] * h, [0] * w
    for y in range(h):
        for x in range(w):
            if pixels[x, y]:
                rows[y] += 1
                cols[x] += 1
    total = sum(rows)
    if not total:
        return 0.0, 1.0, 1.0
    need, tallest = 0.85 * total, h
    for top in range(h):
        held = 0
        for bottom in range(top, h):
            held += rows[bottom]
            if held >= need:
                tallest = min(tallest, bottom - top + 1)
                break
    centre = sum(x * count for x, count in enumerate(cols)) / total
    return total / (w * h), tallest / h, abs(centre - w / 2) / (w / 2)


def subtitled(image: Image.Image | None) -> bool:
    if image is None:
        return False
    share, line, offset = text_line(image)
    return share >= TEXT_SCORE and line <= LINE_HEIGHT and offset <= CENTRE


def _spread(times, count):
    if len(times) <= count:
        return list(times)
    return [times[int(i * len(times) / count)] for i in range(count)]


def sample_times(segments, duration):
    """(moments of speech, moments of silence) away from titles and credits."""
    low, high = SKIP_START, duration - SKIP_END
    if high - low < 600:  # a short film or episode: just skip the edges
        low, high = duration * 0.08, duration * 0.85
    speech = [
        (s.start + s.end) / 2
        for s in segments
        if s.end - s.start >= 1.5 and low < s.start < high and len(s.text.strip()) > 8
    ]
    quiet = [
        (a.end + b.start) / 2
        for a, b in pairwise(segments)
        if b.start - a.end >= 4 and low < a.end < high
    ]
    return _spread(speech, SPEECH_SAMPLES), _spread(quiet, SPEECH_SAMPLES)


def burned_subtitles(path: str, duration: float, segments=None) -> dict:
    """Whether subtitles are drawn into the picture: burned, clean or unsure."""
    speech, quiet = sample_times(segments or [], duration)
    if len(speech) >= 6:
        method, burned_at, clean_below = "speech", BURNED_SHARE, CLEAN_SHARE
    else:
        step = duration * 0.8 / EVEN_SAMPLES
        speech = [duration * 0.1 + step * i for i in range(EVEN_SAMPLES)]
        quiet = []
        method, burned_at, clean_below = "even", EVEN_BURNED_SHARE, EVEN_CLEAN_SHARE
    hits = [t for t in speech if subtitled(grab(path, t))]
    quiet_hits = [t for t in quiet if subtitled(grab(path, t))]
    share = len(hits) / len(speech) if speech else 0.0
    quiet_share = len(quiet_hits) / len(quiet) if quiet else 0.0
    # Subtitles follow the talking; text that's on screen regardless isn't them.
    follows_speech = len(quiet) < 4 or quiet_share <= share - 0.3
    if share >= burned_at and follows_speech:
        verdict = "burned"
    elif share < clean_below:
        verdict = "clean"
    else:
        verdict = "unsure"
    return {
        "verdict": verdict,
        "method": method,
        "hits": len(hits),
        "samples": len(speech),
        "quiet_hits": len(quiet_hits),
        "quiet_samples": len(quiet),
        "at": [round(t, 1) for t in hits[:6]],
    }


def clock(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return (
        f"{hours}:{rest // 60:02}:{rest % 60:02}"
        if hours
        else f"{rest // 60}:{rest % 60:02}"
    )


def _decode_errors(path: str, seconds: float) -> bool:
    try:
        result = subprocess.run(
            [
                required_tool("ffmpeg"),
                "-v",
                "error",
                "-ss",
                f"{seconds:.2f}",
                "-i",
                path,
                "-t",
                "3",
                "-threads",
                "2",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False  # a slow share, not a broken file
    return bool(DECODER_ERROR.search(result.stderr))


def health(
    path: str, duration: float, runtime_minutes, segments, *, film: bool
) -> list:
    """Problems found, each {"kind", "text"} (and "at", seconds, where useful)."""
    issues = []
    if runtime_minutes and duration < SHORT_SHARE * runtime_minutes * 60:
        issues.append(
            {
                "kind": "short",
                "text": f"Runs {round(duration / 60)} min, but it's listed as "
                f"{runtime_minutes} min: it may be cut short or a different version",
            }
        )
    # A subtitle or audio track often runs a little past the picture, so only a
    # picture missing well before the end counts.
    if grab(path, duration * 0.5) is not None and all(
        grab(path, duration * share) is None for share in TRUNCATED_AT
    ):
        issues.append(
            {
                "kind": "truncated",
                "text": "The picture stops before "
                f"{clock(duration * TRUNCATED_AT[0])} of {clock(duration)}: "
                "probably an incomplete download",
            }
        )
    if film and segments and len(segments) >= 20 and duration >= 1200:
        last = max(s.end for s in segments)
        if last < SILENT_END_SHARE * duration:
            issues.append(
                {
                    "kind": "silent_end",
                    "text": f"Nobody speaks after {clock(last)} of {clock(duration)}: "
                    "the sound may cut out",
                    "at": round(last, 1),
                }
            )
    if any(issue["kind"] == "truncated" for issue in issues):
        return issues  # everything past the cut fails to decode; that's the one fault
    points = [duration * (i + 1) / (DECODE_POINTS + 1) for i in range(DECODE_POINTS)]
    bad = [t for t in points if _decode_errors(path, t)]
    if len(bad) >= 2:
        issues.append(
            {
                "kind": "decode_errors",
                "text": "Picture or sound errors at "
                + ", ".join(clock(t) for t in bad),
                "at": round(bad[0], 1),
            }
        )
    return issues
