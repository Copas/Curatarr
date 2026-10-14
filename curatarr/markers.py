"""Previews ("next time on…") and checks on Intro Skipper's markers, from transcripts.

Previews mirror recaps (recaps.py): announced by a line near the end ("Stay
tuned for scenes from our next episode", "Next time on…"), made of clips from
the next episode, so the last line quoted in the next episode anchors the end.

Marker checks hide Intro Skipper segments that are plainly wrong:

- credits shorter than 45 s ending over a minute before another credits marker
  (an 11-second music match under a finale's last scene, real credits later);
- credits in the first quarter of the runtime (opening credits);
- credits under a minute followed by minutes of dialogue (the show isn't over);
- credits that are mostly dialogue (not theme lyrics) with other credits later
  (a dark scene the black-frame check mistook for credits);
- an intro taken from chapter names or an old analysis that's mostly dialogue
  heard in no other episode (theme lyrics repeat every week; dialogue doesn't).

Every rule leaves a marker alone when in doubt.
"""

import re

from .recaps import MAX_RECAP, SNAP_AFTER, SNAP_BEFORE, Recap, quoted

# Reliable in a sample of 1,095 transcripts; bare "next time", "next week" or
# "coming up" are mostly dialogue ("Maybe next time") or sign-offs.
PREVIEW_OPENER = re.compile(
    r"\b(scenes from (our|the) next episode|next time,?\s+on|next week,?\s+on|"
    r"coming up next,?\s+on|on the next episode of|next episode[,:])",
    re.IGNORECASE,
)
PREVIEW_SEARCH = 360.0  # the opener must be this close to the end
PREVIEW_LENGTH = 120.0  # previews are looked for this far after the opener
PREVIEW_TO_END = 20.0  # a preview within this of the end runs to the end
MIN_PREVIEW = 8.0

SHORT_CREDITS = 45.0  # a duplicate this short before the real credits is a false match
DIALOGUE_CREDITS = 60.0  # credits shorter than this are checked for dialogue after
DUPLICATE_GAP = 60.0
EARLY_SHARE = 0.25
DIALOGUE_TAIL = 150.0  # this much episode left after short credits…
DIALOGUE_AFTER = 120.0  # …and this window after them…
DIALOGUE_LINES, DIALOGUE_SECONDS = 4, 20.0  # …holding this much speech
INTRO_DIALOGUE_SHARE = 0.4
INTRO_DIALOGUE_LINES = 3
CHECKED_INTRO_SOURCES = {"Chapter", "Unknown"}
OUTRO_TRANSCRIBE = DIALOGUE_AFTER  # seconds transcribed after a suspect outro


def preview_opener(segments, duration):
    return next(
        (
            s
            for s in segments
            if s.start >= duration - PREVIEW_SEARCH and PREVIEW_OPENER.search(s.text)
        ),
        None,
    )


def find_preview(
    segments,
    duration,
    *,
    later=frozenset(),
    choose_last_line=None,
    confirm=None,
    scenes=(),
) -> Recap | None:
    """The preview's span, or None when there's none or its end is unclear.

    ``later`` is recaps.earlier_grams() of the next episode(s); for the model,
    ``choose_last_line(lines)`` returns the index of the preview's last line and
    ``confirm(lines)`` whether lines are a preview (True/False/None).
    """
    tail = [s for s in segments if s.start >= duration - PREVIEW_SEARCH]
    first = preview_opener(segments, duration)
    if first is None:
        return None
    lines = [s for s in tail if first.start <= s.start <= first.start + PREVIEW_LENGTH]
    start = max(first.start - 0.5, 0.0)
    found = _find_end(start, lines, duration, later, choose_last_line, scenes)
    if found is None or found.source != "model" or confirm is None:
        return found
    span = [s for s in segments if found.start <= s.start < found.end]
    return found if confirm(span) is True else None


def _find_end(start, lines, duration, later, choose_last_line, scenes):
    """Where the preview stops: the model's pick (around the last clip quoted in
    the next episode, when known); without the model, that clip, or the end of
    the file when the preview runs into it."""
    clips = [s for s in lines[1:] if quoted(s, later)] if later else []
    anchor = clips[-1] if clips else None
    candidates = (
        [s for s in lines if anchor.start <= s.start <= anchor.end + 30]
        if anchor
        else lines
    )
    source = "next episode" if anchor else "model"
    end = None
    if choose_last_line is not None:
        try:
            index = choose_last_line(candidates)
        except Exception:  # noqa: BLE001 - fall back to the evidence alone
            index = None
        if isinstance(index, int) and 0 <= index < len(candidates):
            end = candidates[index].end
    if end is None:
        if anchor is not None:
            end = anchor.end
        elif lines[-1].end >= duration - PREVIEW_TO_END:
            end = lines[-1].end
        else:
            return None
    if end >= duration - PREVIEW_TO_END and end >= lines[-1].end:
        return _span(start, duration, "runs to the end", scenes=())
    return _span(start, end, source, scenes)


def _span(start, end, source, scenes) -> Recap | None:
    near = [t for t in scenes if end - SNAP_BEFORE <= t <= end + SNAP_AFTER]
    if near:
        end = min(near, key=lambda t: abs(t - end))
    if not MIN_PREVIEW <= end - start <= MAX_RECAP:
        return None
    return Recap(round(start, 3), round(end, 3), source)


# --- Checks on Intro Skipper's markers ---------------------------------------


def suspect_outros(credits: list[dict], duration: float) -> list[dict]:
    """Short credits ending long before the episode does: worth a dialogue check."""
    return [
        c
        for c in credits
        if c["End"] - c["Start"] < DIALOGUE_CREDITS
        and duration - c["End"] >= DIALOGUE_TAIL
    ]


def bad_outros(
    credits: list[dict], duration: float, speech_after=None, other_grams=None
) -> list[tuple]:
    """(segment, reason) for credits markers that are plainly wrong.

    ``speech_after(start, end)`` returns transcript lines in that span, or None
    when there's no transcript for it (then those rules are skipped);
    ``other_grams`` (recaps.said_in of other episodes) tells theme lyrics from
    dialogue.
    """
    bad = []
    for c in credits:
        length = c["End"] - c["Start"]
        if c["Start"] < duration * EARLY_SHARE:
            bad.append((c, "credits in the first quarter (opening credits)"))
            continue
        later = [
            o for o in credits if o is not c and o["Start"] >= c["End"] + DUPLICATE_GAP
        ]
        if length < SHORT_CREDITS and later:
            bad.append((c, "a short match well before the real credits"))
            continue
        followed = any(o is not c and o["Start"] >= c["End"] for o in credits)
        if followed and speech_after is not None and other_grams:
            inside = speech_after(c["Start"], c["End"])
            unique = [s for s in inside or [] if not quoted(s, other_grams)]
            seconds = sum(
                min(s.end, c["End"]) - max(s.start, c["Start"]) for s in unique
            )
            if (
                len(unique) >= DIALOGUE_LINES
                and seconds >= INTRO_DIALOGUE_SHARE * length
            ):
                bad.append((c, "mostly dialogue, with the real credits later"))
                continue
        if c in suspect_outros(credits, duration) and speech_after is not None:
            lines = speech_after(c["End"], c["End"] + DIALOGUE_AFTER)
            if lines is None:
                continue
            seconds = sum(
                min(s.end, c["End"] + DIALOGUE_AFTER) - max(s.start, c["End"])
                for s in lines
            )
            if len(lines) >= DIALOGUE_LINES and seconds >= DIALOGUE_SECONDS:
                bad.append((c, "dialogue continues for minutes after it"))
    return bad


def bad_intros(intros: list[dict], segments, other_grams: set | None) -> list[tuple]:
    """(segment, reason) for intros that are mostly dialogue no other episode has.

    Only intros from chapter names or old analyses are checked; without other
    episodes to tell theme lyrics from dialogue (``other_grams`` None) none are.
    """
    if not other_grams:
        return []
    bad = []
    for intro in intros:
        if intro.get("Source") not in CHECKED_INTRO_SOURCES:
            continue
        start, end = intro["Start"], intro["End"]
        inside = [s for s in segments if s.start < end and s.end > start]
        unique = [s for s in inside if not quoted(s, other_grams)]
        seconds = sum(min(s.end, end) - max(s.start, start) for s in unique)
        if len(unique) >= INTRO_DIALOGUE_LINES and seconds >= INTRO_DIALOGUE_SHARE * (
            end - start
        ):
            bad.append((intro, "mostly dialogue, not a theme"))
    return bad
