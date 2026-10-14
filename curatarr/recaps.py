"""Find the "previously on" recap at the start of an episode, from its transcript.

A recap is only marked when the episode announces it near the start with one
of the phrases a sample of 1,095 transcripts showed to be reliable: "Previously
on <show>", "Last time/week/season on <show>", "Here's what you missed on…",
"In our last episode". Bare "previously", "last week" or "last time" turn up
in ordinary dialogue too, so they don't count. Where it ends is decided, in
order of confidence, by:

1. The series' closing line. Narrated recaps end the same way every week
   ("…who will be voted out tonight?"), so a phrase that recurs across the
   series' recaps and is always the last recurring phrase marks the end.
2. Lines quoted from earlier episodes. Recaps are cut from earlier episodes;
   the recap ends with the last clip of the run of quoted clips it opens with.
   (Curatarr doesn't let the local model move that end: on The Last Man on
   Earth it pushed every montage recap about 30 seconds into the new episode.
   Ending at the last clip is sometimes a few seconds early, never late.)

Nothing is marked when neither applies: a wrong recap skips part of the
episode, which is worse than having no skip button.
"""

import re
from dataclasses import dataclass

OPENER = re.compile(
    r"\b(previously,?\s+on|last (time|week|season),?\s+on|"
    r"here'?s what you missed|in our last episode)\b",
    re.IGNORECASE,
)
OPENER_WITHIN = 60.0  # the opener must be spoken this early
SEARCH_SECONDS = 240.0  # recaps are looked for this far after the opener
MIN_RECAP = 10.0
MAX_RECAP = 300.0
GRAM = 4
QUOTED = 0.5  # share of a line's 4-word runs found in earlier episodes
EARLIER_TAIL_SKIPPED = 150.0  # "next time on…" previews quote the new episode
AFTER_ANCHOR = 30.0  # the closing narration follows the last quoted clip
QUOTE_RUN_GAP = 30.0  # a recap's clips come one after another; a longer gap ends it
MIN_QUOTE_WORDS = 5  # shorter lines ("So, what do we have?") match by chance
SNAP_BEFORE, SNAP_AFTER = 1.5, 4.0
FUZZY_GRAM, FUZZY_QUOTED = 3, 0.6
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


def grams(text: str, n: int = GRAM) -> set[tuple]:
    w = words(text)
    return {tuple(w[i : i + n]) for i in range(len(w) - n + 1)}


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
                found |= grams(s.text) | grams(s.text, FUZZY_GRAM)
    return found


def said_in(transcripts: list[list]) -> set[tuple]:
    """Everything said in some transcripts, for telling repeats from new lines."""
    found: set[tuple] = set()
    for segments in transcripts:
        for s in segments or []:
            found |= grams(s.text) | grams(s.text, FUZZY_GRAM)
    return found


def quoted(line, other: set[tuple]) -> bool:
    """Whether a line was said in other episodes (``other`` from earlier_grams).

    Four-word runs, or three-word runs when speech recognition heard a word or
    two differently in the two episodes. Short lines never count: four common
    words match some other episode by chance.
    """
    if len(words(line.text)) < MIN_QUOTE_WORDS:
        return False
    g = grams(line.text)
    if g and len(g & other) / len(g) >= QUOTED:
        return True
    g3 = grams(line.text, FUZZY_GRAM)
    return len(g3) >= 3 and len(g3 & other) / len(g3) >= FUZZY_QUOTED


_quoted = quoted


def find_recap(
    segments,
    *,
    series_openings=(),
    earlier=frozenset(),
    choose_last_line=None,
    confirm=None,
    scenes=(),
) -> Recap | None:
    """The recap's span, or None when the episode has none or it's unclear.

    ``series_openings`` are other episodes' ``opening_lines``; ``earlier`` is
    ``earlier_grams`` of the episodes before this one; ``choose_last_line(lines)``
    asks the model for the index of the recap's last line, or None;
    ``confirm(lines)`` asks it whether lines are a recap (True/False, or None
    when it can't say).
    """
    lines = opening_lines(segments)
    if not lines:
        return None
    first = opener(segments)
    start = 0.0 if first.start <= 10 else max(first.start - 0.5, 0.0)
    found = _find_end(start, lines, series_openings, earlier, choose_last_line, scenes)
    if found is None or found.source != "model":
        return found
    # The model chose the end: only keep it if the model also agrees it's a recap.
    if confirm is None:
        return found
    span = [s for s in segments if found.start <= s.start < found.end]
    return found if confirm(span) is True else None


def _find_end(start, lines, series_openings, earlier, choose_last_line, scenes):
    closers = closing_phrases(list(series_openings))
    closing = next((s for s in lines if grams(s.text) & closers), None)
    if closing is not None:
        return _bounded(start, _closing_end(closing, closers), "closing line", scenes)

    anchor = last_of_run(lines, earlier)
    if anchor is None:
        return None
    end, source = spoken_end(anchor), "earlier episodes"
    if choose_last_line is not None:
        after = [
            s for s in lines if anchor.start <= s.start <= anchor.end + AFTER_ANCHOR
        ]
        try:
            index = choose_last_line(after)
        except Exception:  # noqa: BLE001 - the anchor alone is still usable
            index = None
        if isinstance(index, int) and 0 <= index < len(after):
            end, source = spoken_end(after[index]), "model"
    return _bounded(start, end, source, scenes)


def spoken_end(line) -> float:
    """When a line's words stop. Speech recognition sometimes stretches a short
    line over the music after it ("I'm fine. I'm just..." from 1:07 to 3:06),
    so a line ends about half a second per word after it starts, at most."""
    return min(line.end, line.start + 1.0 + 0.5 * max(len(words(line.text)), 1))


def last_of_run(lines, other) -> object | None:
    """The last line of the run of quoted clips that starts the span.

    A recap (or preview) is clip after clip; the run ends at the first gap of
    over 30 seconds between quoted lines, so a stray match later on (a common
    phrase in the new material) can't stretch it.
    """
    if not other:
        return None
    clips = [s for s in lines if quoted(s, other)]
    if not clips or clips[0].start - lines[0].start > OPENER_WITHIN:
        return None
    last = clips[0]
    for clip in clips[1:]:
        if clip.start - last.end > QUOTE_RUN_GAP:
            break
        last = clip
    return last


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
        return spoken_end(line)
    return line.start + (line.end - line.start) * last / len(w) + 1.0


def _bounded(start, end, source, scenes) -> Recap | None:
    near = [t for t in scenes if end - SNAP_BEFORE <= t <= end + SNAP_AFTER]
    if near:
        end = min(near, key=lambda t: abs(t - end))
    if not MIN_RECAP <= end - start <= MAX_RECAP:
        return None
    return Recap(round(start, 3), round(end, 3), source)
