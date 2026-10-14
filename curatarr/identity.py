"""Does an episode's transcript fit its own summary, or another episode's?

A misfiled episode (the Impractical Jokers S11 mix-up, a release named "The
Bart of War" that's really "Brake My Wife, Please") says things another
episode's title and summary describe. Scoring a transcript against every summary
in its season, rather than only its own, keeps vague summaries from causing
alarms: an episode is only flagged when another one wins by a wide margin.

Tested on 277 transcribed episodes: the margin between the best and the own
summary is under 2x for ordinary noise, and every case at 4x or more was a real
problem (a misfiled episode, or a file whose default audio is a dub).
"""

import math
import re
from collections import Counter

STOP_WORDS = """a an the and or but if then of to in on at by for with from as is
are was were be been being it its this that these those he she they we you i his
her their our your my me him them us not no so do does did have has had will
would can could should into about over after before up down out off than too very
just also more most some any all each every one two three what who whom which
when where why how there here"""
STOP = frozenset(STOP_WORDS.split())
MIN_SUMMARIES = 4  # episodes in the season with a usable summary
MIN_SUMMARY_WORDS = 4
WIN_MARGIN = 4.0  # another episode must fit this many times better
MIN_SCORE = 2.0  # …and fit at all


def tokens(text: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[a-z][a-z']+", (text or "").lower())
        if w not in STOP and len(w) > 2
    ]


def scores(transcript_text: str, summaries: dict) -> dict:
    """How well the transcript fits each summary ({key: "title. overview"}).

    Words rare among the season's summaries count most, and a word the episode
    says often counts a little more than one said once.
    """
    said = Counter(tokens(transcript_text))
    usable = {k: v for k, v in summaries.items() if len(tokens(v)) >= MIN_SUMMARY_WORDS}
    if len(usable) < MIN_SUMMARIES:
        return {}
    df = Counter(w for v in usable.values() for w in set(tokens(v)))
    result = {}
    for key, text in usable.items():
        words = set(tokens(text))
        total = sum(
            math.log(len(usable) / df[w]) * (1 + math.log(said[w]))
            for w in words
            if said[w]
        )
        result[key] = total / math.sqrt(len(words))
    return result


def better_match(own, transcript_text: str, summaries: dict):
    """(other key, how many times better) when another episode clearly fits
    the transcript better than its own; otherwise None."""
    fit = scores(transcript_text, summaries)
    if own not in fit:
        return None
    best = max(fit, key=fit.get)
    if best == own or fit[best] < MIN_SCORE:
        return None
    margin = fit[best] / max(fit[own], 0.01)
    return (best, round(margin, 1)) if margin >= WIN_MARGIN else None


def foreign_audio(language: str | None, streams: list[dict], expected="en"):
    """(spoken language, English track available) when the default audio isn't
    the expected language, else None."""
    if not language or language == expected:
        return None
    english = any(
        s.get("codec_type") == "audio"
        and str(s.get("language") or "").lower() in {"eng", "en", "english"}
        for s in streams
    )
    return language, english
