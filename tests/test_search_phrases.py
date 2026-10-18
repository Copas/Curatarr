"""Searching what was said: text in quotes is an exact phrase."""

from curatarr.chapters import Segment
from curatarr.search import TranscriptIndex


def _index():
    transcripts = {
        "hustle": [
            "There's no him reasoning with you.",
            "You are taking us to a very fucking dangerous place",
            "with very serious numbers.",
        ],
        "burnt": [
            "I want my restaurant to be a place where you",
            "fucking... I mean, we should be dealing in culinary orgasms.",
        ],
        "said_it": [
            "Right, that's it.",
            "Fuck this fucking place,",
            "I'm out of here.",
        ],
        "placement": ["Fuck this fucking placement exam."],
    }
    index = TranscriptIndex()
    index.refresh(
        [(key, key) for key in transcripts],
        lambda key: [
            Segment(n * 5.0, n * 5.0 + 4, text)
            for n, text in enumerate(transcripts[key])
        ],
    )
    return index


def test_a_quoted_phrase_must_be_said_exactly():
    hits = _index().search('"fuck this fucking place"')
    assert [h["key"] for h in hits] == ["said_it"]  # not "placement" either
    assert hits[0]["exact"]


def test_curly_quotes_work_too():
    assert [h["key"] for h in _index().search("“fuck this fucking place”")] == [
        "said_it"
    ]


def test_without_quotes_close_matches_still_count():
    keys = {h["key"] for h in _index().search("fucking dangerous place")}
    assert "hustle" in keys


def test_quoted_and_loose_words_together():
    hits = _index().search('"fucking place" out')
    assert [h["key"] for h in hits] == ["said_it"]
