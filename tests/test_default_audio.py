"""A commentary or audio description isn't played by default."""

import subprocess

import pytest
from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import db, default_audio, enrichment
from curatarr.mediafile import probe, tool
from curatarr.models import ChapterState, LifecycleAction


def _audio(index, language="eng", title=None, default=False, extra=False, channels=2):
    return {
        "index": index,
        "codec_type": "audio",
        "language": language,
        "title": title,
        "default": default,
        "extra": extra,
        "channels": channels,
    }


def test_which_track_should_be_the_default():
    choose = default_audio.better_default
    commentary = _audio(1, title="Director's Commentary", default=True)
    assert choose([commentary, _audio(2, channels=6), _audio(3)]) == 2
    described = _audio(1, extra=True, default=True)  # flagged, not named
    assert choose([described, _audio(2)]) == 2
    assert choose([_audio(1, title="AD", default=True), _audio(2)]) == 2
    # Fine already, or nothing better in that language: left alone.
    assert choose([_audio(1, default=True), _audio(2, title="Commentary")]) is None
    assert choose([commentary, _audio(2, language="fre")]) is None
    assert choose([_audio(1, title="Adventure mix", default=True), _audio(2)]) is None


@pytest.mark.skipif(not (tool("ffmpeg") and tool("mkvpropedit")), reason="needs FFmpeg")
def test_the_film_plays_its_own_soundtrack(app, media_env, monkeypatch, tmp_path):  # noqa: F811
    path = str(tmp_path / "Film.mkv")
    subprocess.run(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=64x64",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2:frequency=600",
            "-map",
            "0",
            "-map",
            "1",
            "-map",
            "2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "title=Commentary with the director",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=eng",
            "-disposition:a:0",
            "default",
            "-disposition:a:1",
            "0",
            path,
        ],
        check=True,
    )
    _movie(path)
    monkeypatch.setattr(enrichment, "probe", probe)
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "local_path", lambda p: p)
    monkeypatch.setattr(enrichment, "_refresh_jellyfin", lambda part, media: None)
    monkeypatch.setattr("curatarr.compat_audio._playing", lambda part: False)
    monkeypatch.setattr("curatarr.filecheck_jobs._dry_run", lambda media: False)
    # Not automatic (a new installation): found and listed, the file untouched.
    assert default_audio.sweep() == 0
    (found,) = default_audio.found_rows()
    assert found.default_audio["from"] == "Commentary with the director"
    assert next(s for s in probe(path).streams if s["codec_type"] == "audio")["default"]
    page = app.test_client().get("/chapters/problems").text
    assert (
        "play a commentary, an audio description or another language first" in page
        and "Fix all 1" in page
    )
    assert default_audio.sweep() == 0  # still waiting: not looked at again
    app.test_client().post(f"/chapters/problems/audio/{found.id}/fix")
    assert default_audio.sweep() == 1
    audio = [s for s in probe(path).streams if s["codec_type"] == "audio"]
    assert [s["default"] for s in audio] == [False, True]
    state = db.session.query(ChapterState).one()
    assert state.default_audio["fixed"] and "Commentary" in state.default_audio["from"]
    assert (
        db.session.query(LifecycleAction)
        .filter_by(action_type="default_audio_fixed")
        .count()
        == 1
    )
    assert default_audio.sweep() == 0  # looked at once


def test_which_english_track_should_be_the_default():
    choose = default_audio.english_instead
    japanese = _audio(1, language="jpn", default=True, channels=6)
    assert choose([japanese, _audio(2)]) == 2
    assert choose([japanese, _audio(2, channels=2), _audio(3, channels=6)]) == 3
    # A commentary isn't the English to play; nor is a flag-less guess made
    # about untagged audio, or a change when English is already the default.
    assert choose([japanese, _audio(2, title="Commentary")]) is None
    assert choose([_audio(1, language="und", default=True), _audio(2)]) is None
    assert choose([_audio(1, language=None, default=True), _audio(2)]) is None
    assert choose([_audio(1, default=True), _audio(2, language="jpn")]) is None
    assert choose([japanese, _audio(2, language="fre")]) is None
    assert choose([japanese]) is None
    # No default flag: players start with the first track.
    assert choose([_audio(1, language="tur"), _audio(2)]) == 2
    # English is the default, but another language is ahead of it.
    assert choose([_audio(1, language="jpn"), _audio(2, default=True)]) == 2
    assert choose([_audio(1, language="und"), _audio(2, default=True)]) is None
    # Level on channels: the one a TV can play.
    hard = _audio(2, channels=6) | {"codec_name": "dts"}
    easy = _audio(3, channels=6) | {"codec_name": "ac3"}
    assert choose([japanese, hard, easy]) == 3


def _two_languages(path):
    subprocess.run(
        [tool("ffmpeg"), "-v", "error",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64",
         "-f", "lavfi", "-i", "sine=duration=2",
         "-f", "lavfi", "-i", "sine=duration=2:frequency=600",
         "-map", "0", "-map", "1", "-map", "2",
         "-c:v", "mpeg4", "-c:a", "aac",
         "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng",
         "-disposition:a:0", "default", "-disposition:a:1", "0",
         path],
        check=True,
    )  # fmt: skip


class _Arr:
    def rescan_movie(self, movie_id):
        pass

    def items_trickplay(self, ids):
        return []


def _language_setup(monkeypatch, heard):
    monkeypatch.setattr(enrichment, "probe", probe)
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "local_path", lambda p: p)
    monkeypatch.setattr(enrichment, "_refresh_jellyfin", lambda part, media: None)
    monkeypatch.setattr(enrichment, "_language_model", lambda config, opts: None)
    monkeypatch.setattr("curatarr.compat_audio._playing", lambda part: False)
    monkeypatch.setattr("curatarr.filecheck_jobs._dry_run", lambda media: False)
    monkeypatch.setattr(default_audio, "_automatic", lambda: True)
    monkeypatch.setattr("curatarr.services.client", lambda kind: _Arr())
    monkeypatch.setattr("curatarr.thumbnails.room", lambda **kw: True)
    monkeypatch.setattr("curatarr.thumbnails.has_thumbnails", lambda *a: False)
    monkeypatch.setattr("curatarr.thumbnails.note_rebuilt", lambda *a: None)
    listened = []

    def listen(info, tracks, device, model, make_room=None):
        listened.append(tracks)
        return {track: [(heard, 0.9)] * 5 for track in tracks}

    monkeypatch.setattr(default_audio.dubs, "listen", listen)
    return listened


@pytest.mark.skipif(not (tool("ffmpeg") and tool("mkvmerge")), reason="needs FFmpeg")
def test_english_heard_becomes_the_default(app, media_env, monkeypatch, tmp_path):  # noqa: F811
    path = str(tmp_path / "Film.mkv")
    _two_languages(path)
    _movie(path)
    listened = _language_setup(monkeypatch, "en")
    assert default_audio.sweep() == 0  # nothing changes before it's been heard
    audio = [s for s in probe(path).streams if s["codec_type"] == "audio"]
    assert [s["default"] for s in audio] == [True, False]
    assert default_audio.hear() == 1
    assert listened == [[audio[1]["index"]]]
    # The file is copied in the background, not in the cycle.
    assert default_audio.sweep() == 0
    assert len([s for s in probe(path).streams if s["codec_type"] == "audio"]) == 2
    assert default_audio.move_first() == 1
    audio = [s for s in probe(path).streams if s["codec_type"] == "audio"]
    assert [s["language"] for s in audio] == ["eng", "jpn"]
    assert [s["default"] for s in audio] == [True, False]
    state = db.session.query(ChapterState).one()
    assert state.default_audio["fixed"] and state.default_audio["why"] == "language"
    assert state.compat_audio_size is None
    action = (
        db.session.query(LifecycleAction)
        .filter_by(action_type="default_audio_fixed")
        .one()
    )
    assert "jpn" in action.reason_text and "English" in action.reason_text
    # Done: nothing more to hear or fix, so no loop.
    assert default_audio.hear() == 0
    assert default_audio.sweep() == 0
    assert default_audio.move_first() == 0
    assert len(listened) == 1
    # The TV-friendly copy is looked at again, whatever Radarr says of the codec.
    from curatarr.compat_audio import _due as compat_due

    assert compat_due(set(), set()).count() == 1


@pytest.mark.skipif(not (tool("ffmpeg") and tool("mkvpropedit")), reason="needs FFmpeg")
def test_a_track_not_heard_as_english_is_left_alone(
    app,
    media_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    path = str(tmp_path / "Film.mkv")
    _two_languages(path)
    _movie(path)
    listened = _language_setup(monkeypatch, "ja")
    default_audio.sweep()
    assert default_audio.hear() == 1
    state = db.session.query(ChapterState).one()
    assert state.default_audio["not_english"] == "ja"
    assert default_audio.sweep() == 0
    assert default_audio.hear() == 0  # not listened to again unless it's replaced
    assert len(listened) == 1
    audio = [s for s in probe(path).streams if s["codec_type"] == "audio"]
    assert [s["default"] for s in audio] == [True, False]


@pytest.mark.skipif(not (tool("ffmpeg") and tool("mkvmerge")), reason="needs FFmpeg")
def test_the_english_check_has_its_own_switch_and_asks_when_not_automatic(
    app,
    media_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from curatarr.services import set_setting

    path = str(tmp_path / "Film.mkv")
    _two_languages(path)
    _movie(path)
    _language_setup(monkeypatch, "en")
    monkeypatch.setattr(default_audio, "_automatic", lambda: False)
    default_audio.sweep()
    default_audio.hear()
    (found,) = default_audio.found_rows()
    assert "jpn" in found.default_audio["from"]
    assert default_audio.sweep() == 0  # waiting for Fix
    audio = [s for s in probe(path).streams if s["codec_type"] == "audio"]
    assert audio[0]["default"]
    page = app.test_client().get("/chapters/problems").text
    assert "another language first" in page and "jpn" in page
    app.test_client().post(f"/chapters/problems/audio/{found.id}/fix")
    default_audio.sweep()
    assert default_audio.move_first() == 1

    set_setting("chapter_options", {"english_default_audio": False})
    db.session.commit()
    assert not default_audio.english_enabled()
    assert not default_audio.start()


def test_files_already_looked_at_are_looked_at_again_once(app, media_env):  # noqa: F811
    _movie("/movies/Film (2020)/Film.mkv")
    state = enrichment.state_for(db.session.query(enrichment.MediaPart).one())
    state.default_audio_size = 5
    state.default_audio = {"at": "then"}
    db.session.commit()
    default_audio._look_again_for_language()
    assert state.default_audio_size is None
    state.default_audio_size = 5
    db.session.commit()
    default_audio._look_again_for_language()
    assert state.default_audio_size == 5


@pytest.mark.skipif(not (tool("ffmpeg") and tool("mkvmerge")), reason="needs FFmpeg")
def test_a_playing_file_waits_and_english_ahead_needs_no_copy(
    app,
    media_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    path = str(tmp_path / "Film.mkv")
    _two_languages(path)
    _movie(path)
    _language_setup(monkeypatch, "en")
    default_audio.sweep()
    default_audio.hear()
    default_audio.sweep()
    monkeypatch.setattr("curatarr.compat_audio._playing", lambda part: True)
    assert default_audio.move_first() == 0
    assert db.session.query(ChapterState).one().default_audio["move"]
    monkeypatch.setattr("curatarr.compat_audio._playing", lambda part: False)
    assert default_audio.move_first() == 1
    # English first but another language flagged: only the flag changes.
    from curatarr.mediafile import set_default_audio

    info = probe(path)
    jpn = next(s for s in info.streams if s.get("language") == "jpn")
    set_default_audio(path, info, jpn["index"])
    state = db.session.query(ChapterState).one()
    state.default_audio = {"at": "then", "heard_english": True}
    state.default_audio_size = probe(path).size
    db.session.query(enrichment.MediaPart).one().size_bytes = 1
    db.session.commit()
    assert default_audio.sweep() == 1
    audio = [s for s in probe(path).streams if s["codec_type"] == "audio"]
    assert [(s["language"], s["default"]) for s in audio] == [
        ("eng", True),
        ("jpn", False),
    ]


def test_what_counts_as_hearing_english():
    hears = default_audio.sounds_english
    silent = ("en", 0.541)  # what a clip with no speech comes back as
    assert hears([("en", 0.97), silent, silent, silent, silent])
    assert hears([("en", 0.9)] * 5)
    assert not hears([silent] * 5)
    assert not hears([("en", 0.9), ("ja", 0.9), silent])
    assert not hears([("ja", 0.9)] * 5)
    assert not hears([])
