"""Footage an episode shares with the rest of its season: the intro.

Each episode gets a small fingerprint: one picture a second (a 64-bit
difference hash of a 9x8 grey thumbnail of the last keyframe) and the sound's
Chromaprint fingerprint. A second of an episode is shared with another when

- its picture is found there, *and* a nearby second is found there at the
  matching distance (footage plays in order; one matching frame is chance;
  dark and blank frames don't count), or
- its sound is found there: about a second of Chromaprint values within
  SOUND_ERRORS of the same bits, with a nearby second found at the matching
  distance too. Not identical: the same music under different credits, or
  encoded twice, differs by a few bits, and an exact match broke off part way
  through titles (dark title cards, credits on black).

The intro is the stretch of SHARE or more of the season shared, MIN_LEN to
MAX_LEN seconds long, in the first part of the episode, that overlaps Intro
Skipper's intro most: logo bumpers are shared too but shorter, and a studio
set or a host's opening can be shared for longer, after the titles.

Found on its own, that is sometimes something else (a dark scene after the
titles looks shared), so it's only used to widen Intro Skipper's intro: each
edge moves out to the shared footage's, snapped to a cut, by at most WIDEN
seconds. Intro Skipper's intros usually start a few seconds late and end a few
early. On 28 episodes labelled by eye, its intros were right (both edges
within 3 seconds) on 1 of 17; widened, on 11, and none skips into the show.

Curatarr's own intro (own_intro) goes by the sound alone: the longest stretch
of OWN_MIN to MAX_LEN seconds whose sound OWN_SHARE or more of the season
shares. Pictures add false intros on their own (a dark night shot, a logo),
and a music cue the score repeats is shared by fewer of the season than the
titles are. On 64 episodes of 24 shows, it found all 38 title sequences
(Intro Skipper: 32) and none where there wasn't one, each checked by eye or
by agreeing with Intro Skipper. It's recorded
beside Intro Skipper's for comparison. Where Intro Skipper found no intro,
it's added; where both found one, Intro Skipper's ends where Curatarr's does
(by at most OWN_WIDEN seconds later; titles that close on credits over black
run on past its end), and starts at the earlier of the two (by at most
WIDEN). On 14 intros checked by eye on the live library, it was right on all
(Intro Skipper ended 6-9 seconds early on the 9 both found).

Before one is added, it has to look like a title sequence (titles_evidence):
a music cue the score repeats can be shared by most of a season too, under
different scenes each time, and Skip Intro would skip the story. On the live
library, 16 of 359 added intros were that. A title sequence shows itself one
of three ways: the season shows the same pictures there (TITLE_PICTURES), its
sound is where the other episodes' Intro Skipper intros are
(TITLE_IN_INTROS), or it has lettering on screen, a name or a title
(TITLE_LETTERING). On 24 added intros checked by eye, this kept all 15 title
sequences and refused 8 of the 9 story scenes (in the ninth, sunlit leaves
read as lettering).
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
SOUND_WINDOW = 8  # Chromaprint values compared at a time (about a second)
SOUND_ERRORS = 0.15  # of their bits may differ (unrelated sound: about 0.3)
SOUND_SUPPORT = 2  # seconds to the nearby second found at the same distance
SOUND_SLACK = 1.5  # seconds that distance may differ

SHARE = 0.3  # of the other episodes
BRIDGE = 4  # seconds not shared allowed inside a stretch
MIN_LEN, MAX_LEN = 5, 150  # seconds
EARLY = 0.4  # the intro starts in this first part of the episode…
PRINTED = 0.45  # …so only this first part is fingerprinted
SNAP = 3.0  # an edge moves to the nearest cut this close
WIDEN = 10.0  # Intro Skipper's edges move out at most this far…
OWN_WIDEN = 30.0  # …its end, to the end of Curatarr's own intro, this far
MIN_MOVE = 1.0  # …and at least this far, or they're left as they are
SIBLINGS = 10  # other episodes of the season compared (the nearest)
MIN_SIBLINGS = 3
OWN_SHARE = 0.5  # of the season, typically, has Curatarr's own intro's sound
OWN_MIN = 15  # seconds; bumpers and stings are shorter
OWN_BRIDGE = 6  # seconds not shared allowed inside it
TITLE_PICTURES = 0.1  # of the other episodes show the same pictures there…
TITLE_IN_INTROS = 0.7  # …or have its sound inside their Intro Skipper intro…
TITLE_LETTERING = 3  # …or seconds of it show lettering: it's a title sequence
INTRO_NEAR = 10  # seconds either side of an intro that still count as in it
RUN_ON_SHARE = 0.3  # of the other episodes show the same picture past the end…
RUN_ON_MAX = 30  # …for up to this long, and the intro runs on to there
DIM_MEAN, DIM_STD = 40, 25  # a dark, plain thumbnail (brightness, variation)


@dataclass
class Print:
    hashes: object  # uint64 per second
    flat: object  # bool per second
    sound: object  # uint32 Chromaprint values, or None
    dim: object = None  # bool per second: dark and plain (matches by chance)

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


def _extract(path, seconds):
    """(thumbnails, sound) for the first ``seconds`` of a file: keyframes only,
    about 20 seconds an episode."""
    import numpy as np

    ffmpeg = required_tool("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="curatarr-print-") as folder:
        pics, sound = os.path.join(folder, "p.raw"), os.path.join(folder, "s.fp")
        for args in (
            [ffmpeg, "-v", "error", "-nostdin", "-y", "-skip_frame", "nokey",
             "-t", f"{seconds:.0f}", "-i", path, "-map", "0:V:0", "-an", "-sn", "-dn",
             "-vf", f"fps=1,scale={W}:{H}:flags=area,format=gray",
             "-f", "rawvideo", pics],
            [ffmpeg, "-v", "error", "-nostdin", "-y", "-t", f"{seconds:.0f}",
             "-i", path, "-map", "0:a:0",
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
    """The fingerprint of a file's first part (PRINTED), made once and kept in
    ``cache``."""
    import numpy as np

    key = hashlib.sha256(f"{path}|{round(duration, 1)}".encode()).hexdigest()[:32]
    target = cache / f"{key}.npz"
    try:
        saved = np.load(target)
        thumbnails, sound = saved["pics"], saved["sound"]
    except (OSError, ValueError, KeyError):
        thumbnails, sound = _extract(path, duration * PRINTED + 1)
        cache.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial.npz")
        np.savez(partial, pics=thumbnails, sound=sound)
        os.replace(partial, target)
    hashes, flat = picture_hashes(thumbnails)
    return Print(
        hashes, flat, sound if len(sound) else None, dark_and_plain(thumbnails)
    )


def dark_and_plain(thumbnails):
    """Per second: a dark, plain picture (mean below DIM_MEAN and little
    variation): a night scene or a fade matches the like in any episode."""
    import numpy as np

    t = thumbnails.reshape(len(thumbnails), -1).astype(np.float32)
    return (t.mean(axis=1) < DIM_MEAN) & (t.std(axis=1) < DIM_STD)


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


def _sound_found(here: Print, other: Print, seconds) -> dict:
    """{second: seconds in ``other`` where its sound is} for each second in
    ``seconds`` whose next SOUND_WINDOW Chromaprint values are in ``other``
    with at most SOUND_ERRORS of their bits different."""
    import numpy as np

    if here.sound is None or other.sound is None:
        return {}
    step, theirs = here.sound_step, other.sound
    n = len(theirs) - SOUND_WINDOW + 1
    if n <= 0:
        return {}
    windows = theirs[np.arange(n)[:, None] + np.arange(SOUND_WINDOW)[None, :]]
    most = SOUND_ERRORS * 32 * SOUND_WINDOW
    found = {}
    for i in seconds:
        k = int(i / step)
        mine = here.sound[k : k + SOUND_WINDOW]
        if len(mine) < SOUND_WINDOW:
            continue
        errors = np.bitwise_count(windows ^ mine[None, :]).sum(axis=1)
        at = np.nonzero(errors <= most)[0]
        if len(at):
            found[i] = at * other.sound_step
    return found


def _sound_seconds(here: Print, other: Print, seconds) -> set:
    """Seconds of ``here`` whose sound ``other`` has, backed by a nearby
    second found at the matching distance (one second alike is chance)."""
    import numpy as np

    found = _sound_found(here, other, seconds)
    backed = set()
    for i, js in found.items():
        for di in range(-SOUND_SUPPORT, SOUND_SUPPORT + 1):
            if di == 0 or i + di not in found:
                continue
            diffs = found[i + di][None, :] - js[:, None] - di
            if (np.abs(diffs) <= SOUND_SLACK).any():
                backed.add(i)
                break
    return backed


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


def blocks(shared, bridge=BRIDGE, least=MIN_LEN) -> list[tuple[int, int]]:
    """(start, end) seconds of each stretch shared by SHARE or more."""
    found: list[list[int]] = []
    for i, s in enumerate(shared):
        if s < SHARE:
            continue
        if found and i - found[-1][1] <= bridge:
            found[-1][1] = i
        else:
            found.append([i, i])
    return [(a, b + 1) for a, b in found if b + 1 - a >= least]


def own_intro(here: Print, others: list, duration: float) -> tuple[int, int] | None:
    """Curatarr's own intro, from the sound alone (see the top of this file),
    in seconds, or None when the season shares none."""
    import numpy as np

    if len(others) < MIN_SIBLINGS or here.sound is None:
        return None
    seconds = range(min(int(duration * EARLY), len(here.hashes)))
    shared = np.zeros(len(seconds))
    for other in others:
        for i in _sound_seconds(here, other, seconds):
            shared[i - seconds.start] += 1
    shared /= len(others)
    found = [
        (a, b)
        for a, b in blocks(shared, OWN_BRIDGE, OWN_MIN)
        if b - a <= MAX_LEN and float(np.median(shared[a:b])) >= OWN_SHARE
    ]
    return max(found, key=lambda b: b[1] - b[0], default=None)


def intro_block(
    here: Print, others: list, duration: float, near=None
) -> tuple[int, int] | None:
    """The shared stretch that is the intro, in seconds, or None. With
    ``near`` (Intro Skipper's intro, start and end), the stretch that overlaps
    it most: a show can share longer footage elsewhere early on (a studio
    set, a host's opening), which isn't the intro. Without, the longest."""
    if len(others) < MIN_SIBLINGS:
        return None
    seconds = range(min(int(duration * EARLY), len(here.hashes)))
    found = [b for b in blocks(share(here, others, seconds)) if b[1] - b[0] <= MAX_LEN]
    if near is None:
        return max(found, key=lambda b: b[1] - b[0], default=None)

    def overlap(block):
        return min(block[1], near[1]) - max(block[0], near[0])

    touching = [b for b in found if overlap(b) > 0]
    return max(touching, key=overlap, default=None)


def snap(t: float, breaks) -> float:
    """The picture break (time, kind) nearest ``t`` within SNAP, else ``t``."""
    near = [b for b, _kind in breaks if abs(b - t) <= SNAP]
    return min(near, key=lambda b: abs(b - t)) if near else t


def overlap(a, b) -> float:
    """Seconds two (start, end) spans share (negative: apart)."""
    return min(a[1], b[1]) - max(a[0], b[0])


def widened(theirs, ours, later=WIDEN) -> tuple[float, float] | None:
    """Intro Skipper's intro (start, end) with each edge moved out to ours,
    by MIN_MOVE to WIDEN seconds earlier and ``later`` seconds later; None
    when neither moves (or they don't overlap)."""
    if overlap(theirs, ours) <= 0:
        return None
    start = (
        ours[0] if theirs[0] - WIDEN <= ours[0] <= theirs[0] - MIN_MOVE else theirs[0]
    )
    end = ours[1] if theirs[1] + MIN_MOVE <= ours[1] <= theirs[1] + later else theirs[1]
    if (start, end) == (theirs[0], theirs[1]):
        return None
    return start, end


def picture_share(here: Print, others: list, block) -> float:
    """The share of ``others`` showing each second of ``block``'s picture,
    averaged over its seconds (dark and blank seconds count as not shown)."""
    seconds = range(int(block[0]), int(block[1]))
    if not others or not len(seconds):
        return 0.0
    shown = 0
    for other in others:
        found = _supported(_picture_matches(here, other, seconds))
        shown += sum(1 for i in found if i in seconds)
    return shown / (len(others) * len(seconds))


def sound_in_intros(here: Print, others: list, block) -> tuple[int, int]:
    """(inside, of): of the other episodes, given as (fingerprint, their
    Intro Skipper intros), that have an intro and ``block``'s sound, how many
    have that sound inside their intro."""
    import numpy as np

    seconds = range(int(block[0]), int(block[1]))
    inside = of = 0
    for other, intros in others:
        if not intros:
            continue
        found = _sound_found(here, other, seconds)
        if not found:
            continue
        at = float(np.median(np.concatenate(list(found.values()))))
        of += 1
        inside += any(a - INTRO_NEAR <= at <= b + INTRO_NEAR for a, b in intros)
    return inside, of


def titles_evidence(
    pictures: float, in_intros: tuple[int, int], lettered
) -> str | None:
    """Why a stretch of shared sound is a title sequence ("pictures",
    "intros" or "lettering"), or None: a music cue under the story.
    ``lettered`` is the seconds with lettering, or a function giving them
    (it reads the file, so it's only called when the rest isn't enough)."""
    if pictures >= TITLE_PICTURES:
        return "pictures"
    inside, of = in_intros
    if of and inside / of >= TITLE_IN_INTROS:
        return "intros"
    if (lettered() if callable(lettered) else lettered) >= TITLE_LETTERING:
        return "lettering"
    return None


def run_on(here: Print, others: list, end: float) -> int:
    """Seconds the season's titles carry on past ``end`` with the sound no
    longer shared: the same pictures in RUN_ON_SHARE or more of the others
    (Taskmaster's last shots, the end of The Simpsons' titles before the
    couch gag), up to RUN_ON_MAX. Only a new keyframe counts: these are
    keyframes, and the last one of the titles stands for every second until
    the next, ten seconds into the next scene in some files. A dark, plain
    picture stops it (it matches the like anywhere). What comes after the
    shared pictures, an episode's own title card or couch gag, isn't skipped."""
    import numpy as np

    lo = round(end)
    hi = min(len(here.hashes) - 1, lo + RUN_ON_MAX + 1)
    if hi - lo < 2 or not others:
        return 0
    seconds = range(lo, hi)
    shown = np.zeros(len(seconds))
    for other in others:
        for i in _supported(_picture_matches(here, other, seconds)):
            if lo <= i < hi:
                shown[i - lo] += 1
    shown /= len(others)
    last = 0
    for at in range(len(seconds)):
        i = lo + at
        if shown[at] < RUN_ON_SHARE:
            break
        if here.dim is not None and i < len(here.dim) and here.dim[i]:
            break
        if here.hashes[i] != here.hashes[i - 1]:
            last = at + 1  # a new keyframe the season shares
    return last
