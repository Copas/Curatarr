"""Footage an episode shares with the rest of its season: the intro.

Each episode gets a small fingerprint: one picture a second (a 64-bit
difference hash of a 9x8 grey thumbnail of the last keyframe) and the sound's
Chromaprint fingerprint. A second of an episode is shared with another when

- its picture is found there, *and* a nearby second is found there at the
  matching distance (footage plays in order; one matching frame is chance;
  dark and blank frames don't count), or
- it's inside a run of identical sound.

The intro is the longest stretch of SHARE or more of the season shared, MIN_LEN
to MAX_LEN seconds long, in the first part of the episode: logo bumpers are
shared too but shorter, and a studio set shared for minutes isn't an intro.

Found on its own, that is sometimes something else (a dark scene after the
titles looks shared), so it's only used to widen Intro Skipper's intro: each
edge moves out to the shared footage's, snapped to a cut, by at most WIDEN
seconds. Intro Skipper's intros usually start a few seconds late and end a few
early. On 28 episodes labelled by eye, its intros were right (both edges
within 3 seconds) on 1 of 17; widened, on 11, and none skips into the show.
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import throttle
from .mediafile import MediaToolError, required_tool

W, H = 9, 8  # thumbnail for the difference hash: 8x8 = 64 bits
MAX_BITS = 6  # pictures this close (of 64 bits) are the same picture
FLAT = 6.0  # thumbnails less varied than this are dark or blank
SUPPORT_WITHIN = 4  # seconds to the nearby matching second
SUPPORT_SLACK = 2  # how far its distance may differ (keyframes repeat)
AUDIO_RUN_SECONDS = 1.5  # identical sound for this long is a run
AUDIO_GAP_SECONDS = 1.0

SHARE = 0.3  # of the other episodes
BRIDGE = 4  # seconds not shared allowed inside a stretch
MIN_LEN, MAX_LEN = 5, 150  # seconds
EARLY = 0.4  # the intro starts in this first part of the episode
SNAP = 3.0  # an edge moves to the nearest cut this close
WIDEN = 10.0  # Intro Skipper's edges move out at most this far
SIBLINGS = 10  # other episodes of the season compared (the nearest)
MIN_SIBLINGS = 3


@dataclass
class Print:
    hashes: object  # uint64 per second
    flat: object  # bool per second
    sound: object  # uint32 Chromaprint values, or None

    @property
    def sound_step(self) -> float:
        """Seconds per Chromaprint value (it depends on how ffmpeg ran, so
        it's measured against the pictures, one a second)."""
        return len(self.hashes) / max(len(self.sound), 1)


def picture_hashes(thumbnails):
    """(hashes, flat) for an array of HxW grey thumbnails."""
    import numpy as np

    t = thumbnails.astype(np.int16)
    bits = (t[:, :, 1:] > t[:, :, :-1]).reshape(len(t), -1)
    hashes = np.packbits(bits, axis=1).view(">u8").ravel().astype(np.uint64)
    flat = t.reshape(len(t), -1).std(axis=1) < FLAT
    return hashes, flat


def _extract(path):
    """(thumbnails, sound) for a file: keyframes only, about half a minute an
    episode."""
    import numpy as np

    ffmpeg = required_tool("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="curatarr-print-") as folder:
        pics, sound = os.path.join(folder, "p.raw"), os.path.join(folder, "s.fp")
        for args in (
            [ffmpeg, "-v", "error", "-nostdin", "-y", "-skip_frame", "nokey",
             "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
             "-vf", f"fps=1,scale={W}:{H}:flags=area,format=gray",
             "-f", "rawvideo", pics],
            [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", path, "-map", "0:a:0",
             "-ac", "1", "-ar", "11025", "-f", "chromaprint", "-fp_format", "raw",
             sound],
        ):  # fmt: skip
            result = throttle.run(args, timeout=3600)
            if result.returncode != 0:
                raise MediaToolError(
                    f"Fingerprinting failed: {result.stderr.strip()[-200:]}"
                )
        data = np.fromfile(pics, dtype=np.uint8)
        thumbnails = data[: len(data) // (W * H) * W * H].reshape(-1, H, W)
        return thumbnails, np.fromfile(sound, dtype="<u4")


def fingerprint(path: str, duration: float, cache: Path) -> Print:
    """A file's fingerprint, made once and kept in ``cache``."""
    import numpy as np

    key = hashlib.sha256(f"{path}|{round(duration, 1)}".encode()).hexdigest()[:32]
    target = cache / f"{key}.npz"
    try:
        saved = np.load(target)
        thumbnails, sound = saved["pics"], saved["sound"]
    except (OSError, ValueError, KeyError):
        thumbnails, sound = _extract(path)
        cache.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial.npz")
        np.savez(partial, pics=thumbnails, sound=sound)
        os.replace(partial, target)
    hashes, flat = picture_hashes(thumbnails)
    return Print(hashes, flat, sound if len(sound) else None)


def _picture_matches(here: Print, other: Print, seconds):
    """{second: positions in other} for seconds whose picture ``other`` has."""
    import numpy as np

    found = {}
    usable = ~other.flat
    candidates = other.hashes[usable]
    positions = np.nonzero(usable)[0]
    for i in seconds:
        if i >= len(here.hashes) or here.flat[i]:
            continue
        close = np.bitwise_count(candidates ^ here.hashes[i]) <= MAX_BITS
        if close.any():
            found[i] = positions[close]
    return found


def _supported(matches: dict) -> set:
    """Seconds whose match is backed by a nearby second matching at the
    matching distance."""
    import numpy as np

    supported = set()
    for i, js in matches.items():
        for di in range(-SUPPORT_WITHIN, SUPPORT_WITHIN + 1):
            if di == 0 or i + di not in matches:
                continue
            diffs = matches[i + di][None, :] - js[:, None] - di
            if (np.abs(diffs) <= SUPPORT_SLACK).any():
                supported.add(i)
                break
    return supported


def _sound_seconds(here: Print, other: Print, seconds) -> set:
    """Seconds of ``here`` inside an identical-sound run with ``other``."""
    if here.sound is None or other.sound is None:
        return set()
    step = here.sound_step
    lo = int(seconds.start / step)
    hi = min(len(here.sound), int(seconds.stop / step) + 1)
    index: dict[int, list[int]] = {}
    for j, value in enumerate(other.sound):
        index.setdefault(int(value), []).append(j)
    votes: dict[int, list[int]] = {}
    for i in range(lo, hi):
        for j in index.get(int(here.sound[i]), ()):
            votes.setdefault(j - i, []).append(i)
    run, gap = AUDIO_RUN_SECONDS / step, AUDIO_GAP_SECONDS / step
    found = set()
    for positions in votes.values():
        positions.sort()
        start = prev = positions[0]
        for p in [*positions[1:], 10**9]:
            if p - prev > gap:
                if prev - start >= run:
                    found.update(range(int(start * step), int(prev * step) + 1))
                start = p
            prev = p
    return found


def share(here: Print, others: list, seconds):
    """Per second in ``seconds``, the share of ``others`` that have it."""
    import numpy as np

    holders = np.zeros(len(seconds))
    for other in others:
        found = _supported(_picture_matches(here, other, seconds))
        found |= _sound_seconds(here, other, seconds)
        for i in found:
            if seconds.start <= i < seconds.stop:
                holders[i - seconds.start] += 1
    return holders / max(len(others), 1)


def blocks(shared) -> list[tuple[int, int]]:
    """(start, end) seconds of each stretch shared by SHARE or more."""
    found: list[list[int]] = []
    for i, s in enumerate(shared):
        if s < SHARE:
            continue
        if found and i - found[-1][1] <= BRIDGE:
            found[-1][1] = i
        else:
            found.append([i, i])
    return [(a, b + 1) for a, b in found if b + 1 - a >= MIN_LEN]


def intro_block(here: Print, others: list) -> tuple[int, int] | None:
    """The shared stretch that is the intro, in seconds, or None."""
    if len(others) < MIN_SIBLINGS:
        return None
    seconds = range(int(len(here.hashes) * EARLY))
    found = [b for b in blocks(share(here, others, seconds)) if b[1] - b[0] <= MAX_LEN]
    return max(found, key=lambda b: b[1] - b[0], default=None)


def snap(t: float, breaks) -> float:
    """The picture break (time, kind) nearest ``t`` within SNAP, else ``t``."""
    near = [b for b, _kind in breaks if abs(b - t) <= SNAP]
    return min(near, key=lambda b: abs(b - t)) if near else t


def widened(theirs, ours) -> tuple[float, float] | None:
    """Intro Skipper's intro (start, end) with each edge moved out to ours,
    by at most WIDEN seconds; None when neither moves (or they don't overlap)."""
    if ours[1] <= theirs[0] or ours[0] >= theirs[1]:
        return None
    start = ours[0] if theirs[0] - WIDEN <= ours[0] < theirs[0] - 0.5 else theirs[0]
    end = ours[1] if theirs[1] + 0.5 < ours[1] <= theirs[1] + WIDEN else theirs[1]
    if (start, end) == (theirs[0], theirs[1]):
        return None
    return start, end
