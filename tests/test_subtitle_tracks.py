"""Subtitle tracks in other languages, removed from MKV files."""

import subprocess

import pytest

from curatarr import db, enrichment, mediafile, subtitle_tracks
from curatarr.mediafile import MediaInfo, tool
from curatarr.models import ChapterState, LifecycleAction, MediaPart
from curatarr.services import set_setting

TOOLS = all(tool(name) for name in ("ffmpeg", "mkvmerge", "ffprobe"))


def _info(*subtitles, path="/movies/Film.mkv", size=100):
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "default": True,
        },
    ]
    for n, (language, title) in enumerate(subtitles, start=2):
        streams.append(
            {
                "index": n,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "language": language,
                "title": title,
            }
        )
    return MediaInfo(path, size, 1.0, 60.0, "matroska,webm", [], streams)


def test_kept_languages_and_english_named_untagged_tracks_stay():
    info = _info(
        ("eng", None),  # 2: kept
        ("en", "SDH"),  # 3: kept
        ("spa", None),  # 4
        ("und", "English (Forced)"),  # 5: untagged, named English: kept
        ("und", "Simplified"),  # 6: untagged Chinese, as releases leave them
        (None, None),  # 7: untagged, no name
        ("chi", "English signs"),  # 8: tagged another language: its tag decides
    )
    assert subtitle_tracks.unwanted(info, ["eng"]) == [4, 6, 7, 8]
    assert subtitle_tracks.unwanted(info, ["eng", "spa"]) == [6, 7, 8]
    # "en" is a French word: a French title with it isn't taken as English.
    assert subtitle_tracks.unwanted(_info(("und", "Sous-titres en français")), ["eng"])


@pytest.mark.skipif(not TOOLS, reason="needs FFmpeg and MKVToolNix")
def test_a_real_file_keeps_everything_but_the_other_languages(tmp_path):
    source = str(tmp_path / "a.mp4")
    subprocess.run(
        [tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=160x90:rate=24", "-f", "lavfi", "-i", "sine", "-t", "10",
         "-c:v", "libx264", "-c:a", "aac", source],
        check=True,
    )  # fmt: skip
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n")
    path = str(tmp_path / "Film.mkv")
    subprocess.run(
        [tool("mkvmerge"), "-q", "-o", path, source,
         "--language", "0:eng", str(srt),
         "--language", "0:spa", str(srt),
         "--language", "0:und", "--track-name", "0:Simplified", str(srt)],
        check=True,
    )  # fmt: skip
    info = mediafile.probe(path)
    drop = subtitle_tracks.unwanted(info, ["eng"])
    assert len(drop) == 2

    class Part:
        size_bytes = info.size

    result = subtitle_tracks._rewrite(Part(), path, info, drop)
    assert result["removed"] == 2 and result["languages"] == ["spa", "untagged"]
    after = mediafile.probe(path)
    kinds = [s["codec_type"] for s in after.streams]
    assert kinds == ["video", "audio", "subtitle"]
    assert after.streams[2]["language"] == "eng"
    assert abs(after.duration - info.duration) < 0.5
    assert not list(tmp_path.glob(".*partial"))


def _setup(app, monkeypatch, info):
    from test_enrichment import _movie

    _movie("/movies/Film (2020)/Film.mkv")
    db.session.query(MediaPart).one().size_bytes = 100
    db.session.commit()
    set_setting("chapter_options", {"subtitle_tracks": True})
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "playback_sessions", list)
    monkeypatch.setattr(enrichment, "_refresh_jellyfin", lambda part, media: None)
    monkeypatch.setattr(subtitle_tracks, "probe", lambda p: info)
    monkeypatch.setattr(subtitle_tracks, "file_identity_of", lambda p: (100, 1.0))

    class Radarr:
        def items_trickplay(self, ids):
            return []

        def rescan_movie(self, movie_id):
            pass

    monkeypatch.setattr("curatarr.services.client", lambda kind: Radarr())


def test_the_sweep_cleans_a_file_once(app, monkeypatch):
    info = _info(("eng", None), ("por", None), ("ara", None))
    _setup(app, monkeypatch, info)
    removed = []

    def remove(path, probed, drop, default_audio):
        removed.append((drop, default_audio))
        return MediaInfo(path, 90, 2.0, 60.0, "matroska,webm")

    monkeypatch.setattr(subtitle_tracks, "remove_tracks", remove)
    assert subtitle_tracks.sweep() == 1
    assert removed == [([3, 4], 1)]  # the default audio stays the default
    state = db.session.query(ChapterState).one()
    assert state.subtitle_tracks["removed"] == 2 and state.subtitle_tracks_size == 90
    assert db.session.query(MediaPart).one().size_bytes == 90
    action = (
        db.session.query(LifecycleAction)
        .filter_by(action_type="subtitle_tracks_removed")
        .one()
    )
    assert "ara, por" in action.reason_text
    assert subtitle_tracks.sweep() == 0  # checked; not again unless the file changes
    progress = subtitle_tracks.progress()
    assert progress["rewritten"] == 1 and progress["left"] == 0
    page = app.test_client().get("/chapters").text
    assert "Subtitle tracks in other languages" in page


def test_a_playing_file_waits_and_a_clean_one_is_left_alone(app, monkeypatch):
    info = _info(("eng", None), ("fre", None))
    _setup(app, monkeypatch, info)
    monkeypatch.setattr(
        subtitle_tracks,
        "remove_tracks",
        lambda *a: pytest.fail("rewritten while playing"),
    )
    monkeypatch.setattr("curatarr.seek_repair._playing", lambda part: True)
    assert subtitle_tracks.sweep() == 0
    assert db.session.query(ChapterState).one().subtitle_tracks is None
    info.streams.pop()  # only English left: nothing to do
    assert subtitle_tracks.sweep() == 0
    assert db.session.query(ChapterState).one().subtitle_tracks["ok"]


def test_off_by_default_and_the_languages_are_a_setting(app, client):
    assert not subtitle_tracks.enabled()
    assert subtitle_tracks.languages() == ["eng"]
    from curatarr.chapters import ChapterConfigError, validate_global_options

    assert validate_global_options({"subtitle_languages": ["eng", "spa"]})[
        "subtitle_languages"
    ] == ["eng", "spa"]
    with pytest.raises(ChapterConfigError):
        validate_global_options({"subtitle_languages": ["English"]})
    page = client.get("/chapters/settings").text
    assert "Languages to keep" in page and "Replace chapters that are no use" in page
    # The card's On/Off follows its switch before saving (setting-cards.js).
    assert 'data-toggles="subtitle_tracks"' in page and "setting-cards.js" in page
