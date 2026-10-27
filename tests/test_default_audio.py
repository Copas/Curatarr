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
        "play a commentary or audio description first" in page and "Fix all 1" in page
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
