"""Where an episode's end credits start, from what's on screen.

Intro Skipper finds most credits by their music, but its start is usually a
few seconds late, and it misses credits whose music changes every week. Two
pictures help:

- Credit lettering: thin bright strokes on a darker ground, second after
  second, up to (nearly) the end. The credits are the last long stretch of it.
- Picture breaks: credits nearly always begin at a fade to black or a hard cut,
  so a start is moved back to the break that leads into it.

The start is the earlier of Intro Skipper's start and the lettering's, each
moved to its break. On episodes labelled by eye this put 26 of 33 starts
within 3 seconds (Intro Skipper: 9) and 11 of 16 unseen ones (Intro Skipper:
4), never early: a late start only shows a little of the credits, an early
one would skip the end of the show.

Short end cards with no lettering (a logo, a title) aren't found; Intro
Skipper's marker is then left as it is.
"""

import os
import tempfile
from dataclasses import dataclass

from . import throttle
from .mediafile import MediaToolError, required_tool

TAIL = 360.0  # seconds at the end looked at for lettering
TEXT = 600  # stroke pixels in a frame with credit lettering
GAP = 6  # seconds without, inside the credits (logo cards, a last shot)
MIN_RUN = 10  # seconds of lettering, at least
END_SLACK = 40.0  # the lettering may stop this short of the end (logos)
BACK = 8.0  # a start moves back to a break at most this far…
AHEAD = 0.5  # …or forward this far (further, it can land on a fade between
# credit cards: credits already showing)
TEXT_W, TEXT_H = 480, 270
BREAK_RATE = 10
BREAK_W, BREAK_H = 64, 36
DARK = 12  # a black frame: even its brightest pixels (98th percentile)
BRIGHT = 225  # a white flash: mean brightness
CUT = 28  # mean change from the frame before at a hard cut


@dataclass(frozen=True)
class Found:
    start: float
    how: str  # "lettering" or "Intro Skipper", and the break it moved to


def frames(path, start, seconds, fps, width, height):
    """Grey frames of a stretch of the file, as a (n, height, width) array."""
    import numpy as np

    from . import adaptive

    with tempfile.TemporaryDirectory(prefix="curatarr-frames-") as folder:
        out = os.path.join(folder, "frames.raw")

        def run(hardware):
            return throttle.run(
                [required_tool("ffmpeg"), "-hide_banner", "-nostats", "-nostdin",
                 "-v", "error", "-y"]
                + (["-hwaccel", "auto"] if hardware else [])
                + ["-ss", f"{max(start, 0):.3f}", "-t", f"{seconds:.3f}", "-i", path,
                   "-map", "0:v:0", "-an", "-sn", "-dn",
                   "-vf", f"fps={fps},scale={width}:{height},format=gray",
                   "-f", "rawvideo", out],
                timeout=900,
            )  # fmt: skip

        result = run(hardware=not adaptive.leave_decoder)
        if result.returncode != 0 and not adaptive.leave_decoder:
            result = run(hardware=False)
        if result.returncode != 0:
            raise MediaToolError(
                "Reading the picture failed: "
                + (result.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
            )
        raw = np.fromfile(out, np.uint8)
    size = width * height
    return raw[: len(raw) // size * size].reshape(-1, height, width)


def strokes(pic):
    """Bright pixels with a darker one 1 or 2 pixels away on each side: the
    thin strokes of lettering."""
    import numpy as np

    p = pic.astype(np.int16)
    out = np.zeros(p.shape, bool)
    for d in (1, 2):
        left = np.zeros_like(p)
        left[:, d:] = p[:, :-d]
        for e in (1, 2):
            right = np.zeros_like(p)
            right[:, :-e] = p[:, e:]
            out |= (p >= 150) & (p - left >= 60) & (p - right >= 60)
    return out


def text_score(pic) -> int:
    """Stroke pixels in rows holding at least a few (lines of lettering)."""
    per_row = strokes(pic).sum(axis=1)
    return int(per_row[per_row >= 4].sum())


def last_run(scores, offset, duration) -> float | None:
    """The start of the last stretch of lettering (one score a second from
    ``offset``) that runs to within END_SLACK of the end."""
    runs = []
    for i, score in enumerate(scores):
        if score < TEXT:
            continue
        if runs and i - runs[-1][1] <= GAP:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    runs = [r for r in runs if r[1] - r[0] + 1 >= MIN_RUN]
    if not runs or offset + runs[-1][1] < duration - END_SLACK:
        return None
    return offset + runs[-1][0]


def lettering_start(path, duration) -> float | None:
    start = max(duration - TAIL, 0.0)
    pics = frames(path, start, duration - start, 1, TEXT_W, TEXT_H)
    return last_run([text_score(p) for p in pics], start, duration)


def find_breaks(pics, start) -> list[tuple[float, str]]:
    """(time, "black" | "white" | "cut") for frames at BREAK_RATE from start."""
    import numpy as np

    if not len(pics):
        return []
    pics = pics.astype(np.int16)
    bright = pics.mean(axis=(1, 2))
    top = np.percentile(pics.reshape(len(pics), -1), 98, axis=1)
    change = np.r_[0, np.abs(np.diff(pics, axis=0)).mean(axis=(1, 2))]
    found, n = [], 0
    while n < len(pics):
        for kind, test in (("black", top <= DARK), ("white", bright >= BRIGHT)):
            if test[n]:
                found.append((start + n / BREAK_RATE, kind))
                while n < len(pics) and test[n]:
                    n += 1
                break
        else:
            if change[n] >= CUT:
                found.append((start + n / BREAK_RATE, "cut"))
            n += 1
    return found


def snap(t, found) -> tuple[float, str]:
    """The break leading into ``t``: the nearest fade to black, else white
    flash, else hard cut; ``t`` itself when there's none."""
    for kind in ("black", "white", "cut"):
        near = [b for b, k in found if k == kind and t - BACK <= b <= t + AHEAD]
        if near:
            return min(near, key=lambda b: abs(b - t)), kind
    return t, ""


def snapped(path, t) -> tuple[float, str]:
    a = max(t - BACK, 0.0)
    pics = frames(path, a, BACK + AHEAD, BREAK_RATE, BREAK_W, BREAK_H)
    return snap(t, find_breaks(pics, a))


def credits_start(path, duration, guess=None) -> Found | None:
    """Where the end credits start: the earlier of ``guess`` (Intro Skipper's
    start) and the lettering's, each moved to its break."""
    options = []
    if guess is not None:
        t, kind = snapped(path, guess)
        options.append(
            Found(
                t, f"Intro Skipper's, moved to a {kind}" if kind else "Intro Skipper's"
            )
        )
    letters = lettering_start(path, duration)
    if letters is not None:
        t, kind = snapped(path, letters)
        options.append(
            Found(t, f"credit lettering, from a {kind}" if kind else "credit lettering")
        )
    return min(options, key=lambda f: f.start) if options else None
