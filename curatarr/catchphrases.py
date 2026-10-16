"""Each show's catchphrase: the longest phrase said in many of its episodes.

A show's transcripts are read together. Phrases of three words are counted by
how many episodes say them (once is enough: "the tribe has spoken" comes once
an episode), then grown a word at a time while enough episodes still say the
longer phrase. The longest that's left wins.

Skipped, so the result is the show's own voice and not its packaging or
everyday English:
- the first and last minutes of each episode (theme songs, credits);
- a phrase said at the same point in most episodes (a theme after a cold open);
- a phrase with a word from the show's own name ("Gervais");
- a phrase that starts or ends on a filler word ("guys ready to");
- everyday English ("wait a minute", "first thing in the morning"): a phrase
  said in more than a few of the library's other films and shows.

Shows are worked out in the worker a few at a time, again only when their
transcripts change, and kept in one setting.
"""

import hashlib
import math
import re
import time
from collections import Counter

from . import db
from .models import ChapterState, MediaIdentity, MediaPart

KEY = "catchphrases"
MIN_EPISODES = 3  # transcribed episodes a show needs
MIN_SHARE = 0.2  # of its episodes that must say the phrase
MIN_SAYING = 3  # and at least this many (two is often a "previously on" recap)
MAX_WORDS = 12
EDGE_SECONDS = 180
SAME_SPOT_SECONDS = 45
KEPT = 10  # candidates kept per show, for the library-wide check
SPREAD_KEY = "catchphrase_spread"
SPREAD_EVERY = 3600  # seconds between library-wide checks
# Everyday English: said in at least this share of the other films and shows.
EVERYDAY_SHARE = 0.0075
EVERYDAY_MIN = 3
RUN_SECONDS = 30
# A phrase doesn't start or end on these.
LOOSE_ENDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "but",
        "or",
        "so",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "as",
        "that",
        "than",
        "then",
        "if",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "am",
        "it",
        "its",
        "it's",
        "this",
        "these",
        "those",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "him",
        "us",
        "them",
        "just",
        "like",
    ]
)


def words(text) -> list[str]:
    """Lower-case words, keeping numbers ("5th grade", not "th grade")."""
    return re.findall(r"[a-z0-9']+", (text or "").lower().replace("’", "'"))


def sentences(segments):
    """Each episode as a list of word lists (one per sentence), away from the edges."""
    segments = list(segments)
    ends = max((segment.end for segment in segments), default=0)
    out = []
    for segment in segments:
        if segment.start < EDGE_SECONDS or segment.end > ends - EDGE_SECONDS:
            continue
        said = words(segment.text)
        if said:
            out.append((said, int(segment.start)))
    return out


def _usable(gram, banned) -> bool:
    return not (set(gram) & banned) and all(len(w) > 1 or w in ("a", "i") for w in gram)


def _counts(episodes, length, keep, banned):
    """Episodes saying each phrase of `length` words; `keep` limits which
    (both of its shorter halves must have been common)."""
    counts = Counter()
    for sentences in episodes:
        seen = set()
        for said, _start in sentences:
            for i in range(len(said) - length + 1):
                gram = tuple(said[i : i + length])
                if gram in seen or not _usable(gram, banned):
                    continue
                if keep is not None and (gram[:-1] not in keep or gram[1:] not in keep):
                    continue
                seen.add(gram)
        counts.update(seen)
    return counts


def _reads_as_phrase(gram) -> bool:
    from .trivia import FILLER, STOP

    return (
        gram[0] not in LOOSE_ENDS
        and gram[-1] not in LOOSE_ENDS
        and sum(w not in STOP for w in gram) >= 2
        and not FILLER & set(gram)
    )


def _same_spot(episodes, gram) -> bool:
    """Said at about the same point in most episodes: a theme or an intro."""
    text = " ".join(gram)
    times = []
    for sentences in episodes:
        for said, start in sentences:
            if f" {text} " in f" {' '.join(said)} ":
                times.append(start)
                break
    if len(times) < 3:
        return False
    middle = sorted(times)[len(times) // 2]
    return sum(abs(t - middle) <= SAME_SPOT_SECONDS for t in times) > len(times) / 2


def find(episodes, title) -> list[list]:
    """[phrase, episodes saying it] for a show, longest first.

    episodes: one list per episode of (words, seconds) sentences.
    """
    from .trivia import STOP

    if len(episodes) < MIN_EPISODES:
        return []
    banned = set(words(title)) - STOP
    need = max(MIN_SAYING, math.ceil(MIN_SHARE * len(episodes)))
    common = {}
    keep = None
    for length in range(3, MAX_WORDS + 1):
        counts = {
            g: n
            for g, n in _counts(episodes, length, keep, banned).items()
            if n >= need
        }
        if not counts:
            break
        common.update(counts)
        keep = set(counts)
    ranked = sorted(common.items(), key=lambda kv: (-len(kv[0]), -kv[1], kv[0]))
    chosen = []
    for gram, saying in ranked:
        if not _reads_as_phrase(gram):
            continue
        text = " ".join(gram)
        # Part of a longer phrase already chosen: that one says it better.
        if any(f" {text} " in f" {longer} " for longer, _ in chosen):
            continue
        if _same_spot(episodes, gram):
            continue
        chosen.append((text, saying))
        if len(chosen) >= KEPT:
            break
    return [[text, saying] for text, saying in chosen]


def _shows():
    """{media id: (title, [(state id, transcript key)])} for English episodes."""
    rows = (
        db.session.query(MediaIdentity.id, MediaIdentity.title, ChapterState)
        .join(MediaPart, MediaPart.media_identity_id == MediaIdentity.id)
        .join(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaIdentity.media_type == "series",
            MediaIdentity.missing_since.is_(None),
            MediaPart.kind == "episode",
            ChapterState.transcript_key.isnot(None),
            ChapterState.talk_stats.isnot(None),
        )
        .all()
    )
    shows = {}
    for media_id, title, state in rows:
        if (state.talk_stats or {}).get("language") not in (None, "en"):
            continue
        shows.setdefault(media_id, (title, []))[1].append(state.transcript_key)
    return shows


def _signature(keys) -> str:
    return hashlib.blake2b("".join(sorted(keys)).encode(), digest_size=8).hexdigest()


def sweep() -> int:
    """Work out the catchphrases of shows whose transcripts changed (worker cycle)."""
    from .enrichment import cache_dir
    from .services import set_setting, setting
    from .transcribe import load

    stored = dict(setting(KEY, {}) or {})
    shows = _shows()
    db.session.rollback()  # read the transcripts outside any transaction
    started, done = time.monotonic(), 0
    for media_id, (title, keys) in sorted(shows.items(), key=lambda kv: kv[1][0]):
        signature = _signature(keys)
        if (stored.get(media_id) or {}).get("sig") == signature:
            continue
        episodes = []
        for key in keys:
            hit = load(cache_dir() / "transcripts" / f"{key}.json")
            if hit:
                episodes.append(sentences(hit[0]))
        stored[media_id] = {
            "sig": signature,
            "episodes": len(episodes),
            "phrases": find(episodes, title),
        }
        done += 1
        if time.monotonic() - started > RUN_SECONDS:
            break
    for media_id in set(stored) - set(shows):
        del stored[media_id]  # gone, or no English transcripts left
        done += 1
    spread = setting(SPREAD_KEY) or {}
    stale = time.time() - spread.get("at", 0) > SPREAD_EVERY
    if done and stale or not spread and stored:
        spread = _spread(stored)
        set_setting(SPREAD_KEY, spread)
        done = done or 1
    if done:
        set_setting(KEY, stored)
        db.session.commit()
        from .trivia import _cache

        _cache.clear()
    return done


def _spread(stored) -> dict:
    """How many films and shows say each show's candidate phrases at all."""
    from .enrichment import cache_dir
    from .transcribe import load

    candidates = {
        tuple(phrase.split())
        for show in stored.values()
        for phrase, _ in show.get("phrases") or []
    }
    by_first = {}
    for gram in candidates:
        by_first.setdefault(gram[0], set()).add(len(gram))
    rows = (
        db.session.query(MediaPart.media_identity_id, ChapterState)
        .join(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            ChapterState.transcript_key.isnot(None),
            ChapterState.talk_stats.isnot(None),
        )
        .all()
    )
    files = {}
    for media_id, state in rows:
        if (state.talk_stats or {}).get("language") in (None, "en"):
            files.setdefault(media_id, []).append(state.transcript_key)
    db.session.rollback()
    saying = Counter()
    for keys in files.values():
        found = set()
        for key in keys:
            hit = load(cache_dir() / "transcripts" / f"{key}.json")
            for segment in hit[0] if hit else []:
                said = words(segment.text)
                for i, word in enumerate(said):
                    for length in by_first.get(word, ()):
                        gram = tuple(said[i : i + length])
                        if gram in candidates:
                            found.add(gram)
        saying.update(" ".join(gram) for gram in found)
    return {"at": time.time(), "titles": len(files), "saying": dict(saying)}


def pick(stored, titles, spread=None, watched=None) -> list[dict]:
    """Every show's longest phrase that isn't everyday English: the shows
    watched most first, then the phrases said in the largest share of episodes."""
    spread = spread or {}
    watched = watched or {}
    everyday = max(EVERYDAY_MIN, EVERYDAY_SHARE * spread.get("titles", 0))
    elsewhere = spread.get("saying") or {}
    found = []
    for media_id, show in stored.items():
        if media_id not in titles:
            continue
        for phrase, in_episodes in show.get("phrases") or []:
            # The show itself is one of the titles saying it.
            if elsewhere.get(phrase, 1) - 1 >= everyday:
                continue
            found.append(
                {
                    "media": titles[media_id],
                    "phrase": phrase,
                    "episodes": in_episodes,
                    "of": show["episodes"],
                    "watched": watched.get(media_id, 0),
                }
            )
            break
    found.sort(
        key=lambda f: (-f["watched"], -f["episodes"] / f["of"], f["media"].title)
    )
    return found
