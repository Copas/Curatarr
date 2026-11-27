"""Recaps quote earlier episodes: those without a transcript are read from
their subtitles, and transcripts made in the wrong language don't count."""

import json

from test_recap_recheck import _series

from curatarr import db, enrichment, subtitle_text, transcribe
from curatarr.chapters import Segment
from curatarr.mediafile import MediaInfo

SRT = """1
00:00:01,000 --> 00:00:03,500
[DOOR CREAKS]
SAM: We have to find the way out.

2
00:00:04,000 --> 00:00:06,000
(sighs) Nobody leaves this town.

3
00:00:07,000 --> 00:00:08,000
♪ ♪
"""


def test_subtitles_read_as_what_is_said():
    lines = subtitle_text.segments_from_srt(SRT)
    assert [(s.start, s.text) for s in lines] == [
        (1.0, "We have to find the way out."),
        (4.0, "Nobody leaves this town."),
    ]


def test_a_sidecar_srt_is_read_once_and_kept(tmp_path):
    episode = tmp_path / "Show - S01E01.mkv"
    episode.write_bytes(b"video")
    (tmp_path / "Show - S01E01.en.srt").write_text(SRT, encoding="utf-8")
    cache = tmp_path / "cache"
    first = subtitle_text.transcript(str(episode), cache)
    assert [s.text for s in first] == [
        "We have to find the way out.",
        "Nobody leaves this town.",
    ]
    (tmp_path / "Show - S01E01.en.srt").unlink()
    assert subtitle_text.transcript(str(episode), cache) == first  # kept


def test_earlier_episodes_without_a_transcript_are_read_from_their_subtitles(
    app, monkeypatch
):
    """FROM S4E1 came with good subtitles, so only its opening was
    transcribed; S4E2's recap of it matched nothing and stopped after 19 s.
    Episode 2 here was transcribed in French (Whisper's guess on an English
    track): its subtitles stand in for it too."""
    english = [Segment(10.0, 12.0, "Something happened out there tonight.")]
    states, _ = _series({1: (None, None), 2: (None, english), 3: (None, english)})
    kept = {
        "t2": (english, {"language": "fr"}),
        "t3": (english, {"language": "en"}),
    }
    monkeypatch.setattr(enrichment, "_transcript_and_details", kept.get)
    read = []

    def subtitles(part):
        read.append(part.episode_number)
        return [Segment(5.0, 7.0, f"Words from episode {part.episode_number}.")]

    monkeypatch.setattr(enrichment, "_subtitle_transcript", subtitles)
    state = states[3]
    _openings, earlier = enrichment._sibling_transcripts(state, state.part)
    assert sorted(read) == [1, 2]  # the latest first, both without usable words
    said = {s.text for words in earlier for s in words}
    assert said == {"Words from episode 1.", "Words from episode 2."}


def _info(tag):
    streams = [{"index": 1, "codec_type": "audio", "default": True, "language": tag}]
    return MediaInfo(
        "/mnt/media/Show/S01E03.mkv", 1, 1.0, 2700.0, "matroska", [], streams
    )


def test_the_track_tag_names_the_language_not_whispers_guess(tmp_path):
    assert transcribe.track_language(_info("eng")) == "en"
    assert transcribe.track_language(_info("und")) is None
    assert transcribe.wrong_language({"language": "fr"}, _info("eng"))
    assert not transcribe.wrong_language({"language": "ja"}, _info("jpn"))
    assert not transcribe.wrong_language({"language": "fr"}, _info(None))
    # a saved transcript in the wrong language counts as none: it's made again
    info = _info("eng")
    key = transcribe.cache_key(info, "medium")
    rows = {"segments": [{"start": 1, "end": 2, "text": "Oh c'est pas vrai"}]}
    (tmp_path / f"{key}.json").write_text(
        json.dumps({**rows, "details": {"language": "fr"}}), encoding="utf-8"
    )
    assert transcribe.cached(info, "medium", tmp_path) is None
    (tmp_path / f"{key}.json").write_text(
        json.dumps({**rows, "details": {"language": "en"}}), encoding="utf-8"
    )
    assert transcribe.cached(info, "medium", tmp_path) is not None


def test_reading_a_track_goes_through_the_pause_while_watching(tmp_path, monkeypatch):
    """Reading a subtitle track reads the whole file from the share; run
    directly, hundreds of them starved a stream someone was watching. They
    go through throttle, which holds and suspends them during playback."""
    import subprocess

    episode = tmp_path / "Show - S01E02.mkv"
    episode.write_bytes(b"video")
    ran = []

    def run(args, timeout, env=None):
        ran.append(args[0])
        if "ffprobe" in args[0]:
            out = json.dumps(
                {
                    "streams": [
                        {
                            "index": 3,
                            "codec_name": "subrip",
                            "tags": {"language": "eng", "title": "SDH"},
                        },
                        {
                            "index": 2,
                            "codec_name": "subrip",
                            "tags": {"language": "eng"},
                        },
                    ]
                }
            )
        else:
            assert args[args.index("-map") + 1] == "0:2"  # the plain track
            out = SRT
        return subprocess.CompletedProcess(args, 0, out, "")

    waited = []
    monkeypatch.setattr(subtitle_text.throttle, "run", run)
    monkeypatch.setattr(
        subtitle_text.throttle, "wait_while_paused", lambda: waited.append(1)
    )
    monkeypatch.setattr(subtitle_text, "tool", lambda name: f"/usr/bin/{name}")
    found = subtitle_text.transcript(str(episode), tmp_path / "cache")
    assert found[0].text == "We have to find the way out."
    assert ran == ["/usr/bin/ffprobe", "/usr/bin/ffmpeg"] and len(waited) == 2


def test_reading_earlier_episodes_leaves_the_database_free(app, monkeypatch):
    """The first lookup wrote the job's changes so far, and the write was
    held while dozens of transcripts were read (38 s on a long series)."""
    from conftest import database_free

    english = [Segment(10.0, 12.0, "Something happened out there tonight.")]
    states, _ = _series({1: (None, None), 2: (None, english)})
    free = []

    def subtitles(part):
        free.append(database_free(app))
        return []

    monkeypatch.setattr(enrichment, "_transcript_and_details", lambda key: None)
    monkeypatch.setattr(enrichment, "_subtitle_transcript", subtitles)
    state = states[2]
    db.session.expire(state.part)  # as after a commit: the next look reloads it
    state.attempts = 5  # a change the job made before
    enrichment._sibling_transcripts(state, state.part)
    assert free == [True]
