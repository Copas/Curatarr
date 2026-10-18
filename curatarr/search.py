"""Find episodes by what's said in them, from Curatarr's saved transcripts.

"Which episode had the line about the stapler?" Each transcript is searched in
windows of a few consecutive lines; an exact phrase match ranks first, then the
share of the query's words said in that window. Only the best moment of each
episode is returned.
"""

import math
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

WINDOW = 3  # consecutive transcript lines searched together
# "Exact words" in straight or curly double quotes.
QUOTED = re.compile(r'["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]')
MIN_SHARE = 0.75  # of the query's words, when the exact phrase isn't said
STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "it",
        "its",
        "this",
        "that",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "not",
        "no",
        "so",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "just",
    ]
)
REFRESH_SECONDS = 300


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower().replace("’", "'"))


def stem(word: str) -> str:
    """A rough stem, the same for "vote", "voted", "votes" and "voting"."""
    word = _stem(word)
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def _stem(word: str) -> str:
    """A rough stem, so "frying" finds "fry", "turkeys" "turkey", "tribes" "tribe"."""
    if len(word) <= 3:
        return word
    for suffix, keep, shortest in (("ying", "y", 2), ("ies", "y", 2), ("ied", "y", 2)):
        if word.endswith(suffix) and len(word) - len(suffix) >= shortest:
            return word[: -len(suffix)] + keep
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            if word[-1] == word[-2] and word[-1] not in "aeiouls":
                word = word[:-1]  # "stopped" -> "stopp" -> "stop"
            return word
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]  # "glasses", "boxes", "watches"
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


@dataclass
class Entry:
    key: str  # whatever identifies the episode to the caller
    lines: list  # transcript segments (start, end, text)
    normalized: list[str]  # each window's text, lower-case words joined by spaces
    transcript: str = ""  # the transcript it was loaded from
    vocabulary: frozenset = frozenset()  # every word said in the episode


def _windows(lines) -> list[str]:
    flat = [" ".join(words(s.text)) for s in lines]
    return [" ".join(flat[i : i + WINDOW]) for i in range(len(flat))]


class TranscriptIndex:
    """Transcripts held in memory, reloaded when new ones appear."""

    def __init__(self):
        self._entries: dict[str, Entry] = {}
        self._lock = threading.Lock()
        self._checked = 0.0

    def refresh(self, sources: Iterable[tuple[str, str]], load) -> None:
        """``sources`` are (episode key, transcript key); ``load(transcript key)``
        returns its segments or None."""
        with self._lock:
            if time.monotonic() - self._checked < REFRESH_SECONDS and self._entries:
                return
            wanted = dict(sources)
            for key in list(self._entries):
                if key not in wanted:
                    del self._entries[key]
            for key, transcript in wanted.items():
                entry = self._entries.get(key)
                if entry is not None and entry.transcript == transcript:
                    continue
                lines = load(transcript)
                if lines:
                    normalized = _windows(lines)
                    vocabulary = frozenset(
                        stem(w) for window in normalized for w in window.split()
                    )
                    self._entries[key] = Entry(
                        key, lines, normalized, transcript, vocabulary
                    )
            self._checked = time.monotonic()

    def __len__(self):
        return len(self._entries)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Best moment per episode. Text in quotes must be said exactly as
        written (whole words, in order); the rest ranks as usual."""
        quoted = [" ".join(words(q)) for q in QUOTED.findall(query) if words(q)]
        loose = QUOTED.sub(" ", query) if quoted else query
        phrase = " ".join(words(query))
        wanted = [stem(w) for w in words(loose) if w not in STOP] or [
            stem(w) for w in words(loose)
        ]
        if not wanted and not quoted:
            return []
        quoted_stems = {stem(w) for q in quoted for w in q.split()}
        hits = []
        need = max(1, math.ceil(MIN_SHARE * len(wanted))) if wanted else 0
        for entry in list(self._entries.values()):
            # Skip episodes that never say enough of the words (most of them).
            if not quoted_stems <= entry.vocabulary:
                continue
            if sum(w in entry.vocabulary for w in wanted) < need:
                continue
            best = None
            for index, window in enumerate(entry.normalized):
                padded = f" {window} "
                if quoted and not all(f" {q} " in padded for q in quoted):
                    continue
                if quoted and not wanted or phrase and f" {phrase} " in padded:
                    score = 2.0
                else:
                    present = {stem(w) for w in window.split()}
                    share = sum(w in present for w in wanted) / len(wanted)
                    if share < MIN_SHARE:
                        continue
                    score = share
                if best is None or score > best[0]:
                    best = (score, index)
            if best:
                score, index = best
                around = entry.lines[max(index - 1, 0) : index + WINDOW + 1]
                hits.append(
                    {
                        "key": entry.key,
                        "score": round(score, 2),
                        "exact": score >= 2.0,
                        "at": entry.lines[index].start,
                        "lines": [s.text.strip() for s in around],
                    }
                )
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]


index = TranscriptIndex()


def highlight(line: str, query: str):
    """A result line with the searched words marked (HTML-safe): every word
    of a quoted phrase, and the other words (not "the", "and"...) in any form
    ("vote" marks "voted")."""
    from markupsafe import Markup, escape

    quoted = {stem(w) for q in QUOTED.findall(query) for w in words(q)}
    loose = words(QUOTED.sub(" ", query))
    wanted = {stem(w) for w in loose if w not in STOP} or {stem(w) for w in loose}
    marked = quoted | wanted
    out, last = [], 0
    for match in re.finditer(r"[A-Za-z0-9'’]+", line):
        word = match.group(0).lower().replace("’", "'")
        if stem(word) in marked or stem(word.strip("'")) in marked:
            out.append(escape(line[last : match.start()]))
            out.append(Markup("<mark>") + escape(match.group(0)) + Markup("</mark>"))
            last = match.end()
    out.append(escape(line[last:]))
    return Markup("").join(out)
