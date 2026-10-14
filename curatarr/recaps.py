"""Find the "previously on" recap at the start of an episode, from its transcript.

A recap is only marked when the episode says so ("Previously on…", "Last time
on…") near the start. Where it ends is decided, in order of confidence, by:

1. The series' closing line. Narrated recaps end the same way every week
   ("…who will be voted out tonight?"), so a phrase that recurs across the
   series' recaps and is always the last recurring phrase marks the end.
2. Lines quoted from earlier episodes. Recaps are cut from earlier episodes;
   the last quoted line anchors the end, and the model picks the recap's last
   line among the few after it (the narrator's closing words).

Nothing is marked when neither applies: a wrong recap skips part of the episode,
which is worse than having no skip button.
"""

import re
from dataclasses import dataclass

OPENER = re.compile(
    r"\b(previously on|last time on|last week on|previously)\b", re.IGNORECASE
)
OPENER_WITHIN = 60.0  # the opener must be spoken this early
SEARCH_SECONDS = 240.0  # recaps are looked for this far after the opener
MIN_RECAP = 10.0
MAX_RECAP = 300.0
GRAM = 4
QUOTED = 0.5  # share of a line's 4-word runs found in earlier episodes
EARLIER_TAIL_SKIPPED = 150.0  # "next time on…" previews quote the new episode
AFTER_ANCHOR = 75.0  # the closing narration follows the last quoted clip
SNAP_BEFORE, SNAP_AFTER = 1.5, 4.0
CLOSER_WITHIN = 180.0  # seconds after the opener's next line
CLOSER_SPREAD = 120.0  # a closing line lands at about the same point each week
CLOSER_MIN_RUNS = 3  # shared 4-word runs in one line: a 6+ word shared sentence


@dataclass(frozen=True)
class Recap:
    start: float
    end: float
    source: str  # "closing line" | "earlier episodes" | "model"

    def as_dict(self):
        return {"start": self.start, "end": self.end, "source": self.source}


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def grams(text: str) -> set[tuple]:
    w = words(text)
    return {tuple(w[i : i + GRAM]) for i in range(len(w) - GRAM + 1)}


def opener(segments):
    return next(
        (s for s in segments if s.start <= OPENER_WITHIN and OPENER.search(s.text)),
        None,
    )


def opening_lines(segments) -> list | None:
    """The lines after an episode's recap opener, or None without one."""
    first = opener(segments)
    if first is None:
        return None
    return [
        s
        for s in segments
        if s is not first and first.start < s.start <= first.start + SEARCH_SECONDS
    ]


def closing_phrases(openings: list[list]) -> set[tuple]:
    """4-word runs that end the series' recaps.

    A closing line is a whole sentence said once per recap, early, at about the
    same point every week ("…12 are left. Who will be voted out tonight?").
    Everyday phrases ("I don't know") recur too, but they're short, repeat within
    an episode or wander, so they don't qualify; nor do short mid-recap repeats
    like "the tribe has spoken".
    """
    openings = [lines for lines in openings if lines]
    if len(openings) < 3:
        return set()
    where: dict[tuple, list[tuple[int, float]]] = {}  # gram -> (episode, offset)
    repeated: set[tuple] = set()
    for episode, lines in enumerate(openings):
        origin = lines[0].start
        seen: set[tuple] = set()
        for line in lines:
            for gram in grams(line.text):
                if gram in seen:
                    repeated.add(gram)
                    continue
                seen.add(gram)
                where.setdefault(gram, []).append((episode, line.start - origin))
    # A third, not half: long-running shows change their closing line between
    # seasons (Survivor did), so no single closer covers most of the run.
    need = max(3, -(-len(openings) // 3))
    candidates = {
        gram
        for gram, places in where.items()
        if gram not in repeated
        and len(places) >= need
        and max(offset for _e, offset in places) <= CLOSER_WITHIN
        and max(o for _e, o in places) - min(o for _e, o in places) <= CLOSER_SPREAD
    }
    # A closing line is a whole shared sentence ("… are left. Who will be voted
    # out tonight?"), so its runs come in a chain; a stray shared phrase is one run.
    long_lines = set()
    for lines in openings:
        for line in lines:
            shared = grams(line.text) & candidates
            if len(shared) >= CLOSER_MIN_RUNS:
                long_lines |= shared
    # Long shared sentences that come later (often the theme song's lyrics right
    # after the recap) don't move the end: find_recap uses the first one.
    closers = candidates & long_lines
    return closers


def earlier_grams(transcripts: list[list]) -> set[tuple]:
    """What was said in earlier episodes, minus their "next time on" previews."""
    found: set[tuple] = set()
    for segments in transcripts:
        if not segments:
            continue
        cutoff = segments[-1].end - EARLIER_TAIL_SKIPPED
        for s in segments:
            if s.start < cutoff:
                found |= grams(s.text)
    return found


def _quoted(line, earlier: set[tuple]) -> bool:
    g = grams(line.text)
    return bool(g) and len(g & earlier) / len(g) >= QUOTED


def find_recap(
    segments,
    *,
    series_openings=(),
    earlier=frozenset(),
    choose_last_line=None,
    scenes=(),
) -> Recap | None:
    """The recap's span, or None when the episode has none or it's unclear.

    ``series_openings`` are other episodes' ``opening_lines``; ``earlier`` is
    ``earlier_grams`` of the episodes before this one; ``choose_last_line(lines)``
    asks the model for the index of the recap's last line, or None.
    """
    lines = opening_lines(segments)
    if not lines:
        return None
    first = opener(segments)
    start = 0.0 if first.start <= 10 else max(first.start - 0.5, 0.0)

    closers = closing_phrases(list(series_openings))
    closing = next((s for s in lines if grams(s.text) & closers), None)
    if closing is not None:
        return _bounded(start, _closing_end(closing, closers), "closing line", scenes)

    quoted = [s for s in lines if _quoted(s, earlier)] if earlier else []
    if not quoted:
        return None
    anchor = quoted[-1]
    end, source = anchor.end, "earlier episodes"
    if choose_last_line is not None:
        after = [
            s for s in lines if anchor.start <= s.start <= anchor.end + AFTER_ANCHOR
        ]
        try:
            index = choose_last_line(after)
        except Exception:  # noqa: BLE001 - the anchor alone is still usable
            index = None
        if isinstance(index, int) and 0 <= index < len(after):
            end, source = after[index].end, "model"
    return _bounded(start, end, source, scenes)


def _closing_end(line, closers) -> float:
    """Where the closing phrase stops, when the transcript ran it into the next line.

    Speech recognition sometimes merges the closer with the new episode's first
    words ("…voted out tonight i dreamed i got shot"); estimate the closer's end
    from its position among the line's words.
    """
    w = words(line.text)
    last = max(
        (
            i + GRAM
            for i in range(len(w) - GRAM + 1)
            if tuple(w[i : i + GRAM]) in closers
        ),
        default=len(w),
    )
    if last >= len(w) - 1:
        return line.end
    return line.start + (line.end - line.start) * last / len(w) + 1.0


def _bounded(start, end, source, scenes) -> Recap | None:
    near = [t for t in scenes if end - SNAP_BEFORE <= t <= end + SNAP_AFTER]
    if near:
        end = min(near, key=lambda t: abs(t - end))
    if not MIN_RECAP <= end - start <= MAX_RECAP:
        return None
    return Recap(round(start, 3), round(end, 3), source)
