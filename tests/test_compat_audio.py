"""TV-friendly audio: an AC3 track beside audio many TVs can't play."""

import subprocess

import pytest

from curatarr import compat_audio, db, enrichment, mediafile
from curatarr.mediafile import MediaInfo, compatible_audio_source, tool
from curatarr.models import ChapterState, LifecycleAction


def _info(*audio):
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    for n, (codec, language, default) in enumerate(audio, 1):
        streams.append(
            {
                "index": n,
                "codec_type": "audio",
                "codec_name": codec,
                "language": language,
                "default": default,
            }
        )
    return MediaInfo("/f.mkv", 1, 1.0, 60.0, "matroska,webm", streams=streams)


def test_only_hard_main_audio_without_an_easy_twin_gets_a_track():
    assert compatible_audio_source(_info(("dts", "eng", True))) == 1
    assert (
        compatible_audio_source(_info(("truehd", "eng", True), ("ac3", "eng", False)))
        is None
    )
    assert (
        compatible_audio_source(_info(("eac3", "eng", True), ("dts", "eng", False)))
        is None
    )
    assert (
        compatible_audio_source(_info(("aac", "eng", False), ("dts", "eng", True)))
        is None
    )
    assert (
        compatible_audio_source(_info(("dts", "eng", True), ("ac3", "fre", False))) == 1
    )


@pytest.mark.skipif(not tool("ffmpeg"), reason="needs FFmpeg")
def test_the_track_is_added_first_and_everything_else_kept(tmp_path):
    path = str(tmp_path / "film.mkv")
    subprocess.run(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "4",
            "-ac",
            "6",
            "-c:v",
            "libx264",
            "-c:a",
            "flac",
            "-metadata:s:a:0",
            "language=eng",
            path,
        ],
        check=True,
    )
    before = mediafile.probe(path)
    source = compatible_audio_source(before)
    after = mediafile.add_compatible_audio(path, before, source)
    audio = [s for s in after.streams if s["codec_type"] == "audio"]
    assert [s["codec_name"] for s in audio] == ["ac3", "flac"]
    assert audio[0]["default"] and not audio[1]["default"]
    assert audio[0]["language"] == "eng"
    assert abs(after.duration - before.duration) < 0.5
    assert not list(tmp_path.glob(".*partial"))


class Radarr:
    def movies(self):
        return [
            {"id": 1, "movieFile": {"mediaInfo": {"audioCodec": "DTS-HD MA"}}},
            {"id": 2, "movieFile": {"mediaInfo": {"audioCodec": "EAC3"}}},
        ]

    def items_trickplay(self, ids):
        return []

    def rescan_movie(self, movie_id):
        pass


def test_the_sweep_does_hard_films_once(app, monkeypatch):
    from test_enrichment import _movie

    from curatarr.models import MediaIdentity

    _movie("/movies/Loud (2020)/Loud.mkv")
    db.session.query(MediaIdentity).one().radarr_id = 1  # Radarr says DTS-HD
    db.session.commit()
    monkeypatch.setattr("curatarr.services.client", lambda kind: Radarr())
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "playback_sessions", list)
    monkeypatch.setattr(enrichment, "_refresh_jellyfin", lambda part, media: None)
    monkeypatch.setattr("curatarr.filecheck_jobs._dry_run", lambda media: False)
    monkeypatch.setattr(compat_audio, "probe", lambda path: _info(("dts", "eng", True)))
    monkeypatch.setattr(compat_audio, "can_write", lambda info: (True, None))
    monkeypatch.setattr(compat_audio, "tv_enabled", lambda: True)
    calls = []

    def add(path, info, source):
        calls.append(source)
        return _info(("ac3", "eng", True), ("dts", "eng", False))

    monkeypatch.setattr(compat_audio, "add_compatible_audio", add)
    assert compat_audio.sweep() == 1
    assert calls == [1]
    state = db.session.query(ChapterState).one()
    assert state.compat_audio["added"] and state.compat_audio["from"] == "dts"
    assert (
        db.session.query(LifecycleAction)
        .filter_by(action_type="compat_audio_added")
        .count()
        == 1
    )
    assert compat_audio.sweep() == 0  # not done twice


def test_off_by_default(app):
    assert not compat_audio.enabled()
    assert not compat_audio.start()


class Sonarr:
    def series(self):
        return [{"id": 5}]

    def episode_files(self, series_id):
        return [
            {"id": 51, "mediaInfo": {"audioCodec": "DTS"}},
            {"id": 52, "mediaInfo": {"audioCodec": "EAC3"}},
        ]


def test_episodes_with_dts_are_found_from_sonarr(app, monkeypatch):
    clients = {"radarr": Radarr(), "sonarr": Sonarr()}
    monkeypatch.setattr("curatarr.services.client", lambda kind: clients[kind])
    monkeypatch.setattr("curatarr.services.configured", lambda kind: True)
    monkeypatch.setattr(compat_audio, "tv_enabled", lambda: True)
    films, episodes = compat_audio.hard_files()
    assert films == {1} and episodes == {51}
    # AAC for browsers (with the plugin) also takes E-AC3 and AC3.
    compat_audio._sonarr_cache.clear()
    monkeypatch.setattr(compat_audio, "browser_enabled", lambda: True)
    films, _episodes = compat_audio.hard_files()
    assert films == {1, 2}


def test_progress_shows_on_enhance_and_overview(app, client, monkeypatch):
    from test_enrichment import _movie

    from curatarr.models import MediaPart
    from curatarr.services import set_setting

    assert compat_audio.progress() is None  # off, nothing done
    _movie("/movies/Loud (2020)/Loud.mkv")
    state = enrichment.state_for(db.session.query(MediaPart).one())
    state.compat_audio = {"added": True, "from": "dts"}
    set_setting(compat_audio.REMAINING_KEY, {"films": 3, "episodes": 10})
    db.session.commit()
    monkeypatch.setattr(compat_audio, "enabled", lambda: True)
    monkeypatch.setattr(compat_audio, "tv_enabled", lambda: True)
    found = compat_audio.progress()
    assert found["films"] == {"done": 1, "total": 4, "left": 3}
    assert found["episodes"]["total"] == 10
    assert "Playable audio: films" in client.get("/chapters").text


def test_a_busy_database_doesnt_lose_an_added_track(app, monkeypatch):
    """The file is already rewritten; Radarr then reports the new track, so
    the film would never be looked at again."""
    from sqlalchemy.exc import OperationalError
    from test_enrichment import _movie

    from curatarr.models import MediaIdentity

    _movie("/movies/Loud (2020)/Loud.mkv")
    db.session.query(MediaIdentity).one().radarr_id = 1
    db.session.commit()
    monkeypatch.setattr("curatarr.services.client", lambda kind: Radarr())
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "playback_sessions", list)
    monkeypatch.setattr(enrichment, "_refresh_jellyfin", lambda part, media: None)
    monkeypatch.setattr("curatarr.filecheck_jobs._dry_run", lambda media: False)
    monkeypatch.setattr(compat_audio, "probe", lambda path: _info(("dts", "eng", True)))
    monkeypatch.setattr(compat_audio, "can_write", lambda info: (True, None))
    monkeypatch.setattr(compat_audio, "tv_enabled", lambda: True)
    monkeypatch.setattr(
        compat_audio,
        "add_compatible_audio",
        lambda path, info, source: _info(("ac3", "eng", True), ("dts", "eng", False)),
    )
    real, tries = compat_audio._record, []

    def busy_once(state, part, result):
        tries.append(1)
        if len(tries) == 1:
            state.compat_audio = {"half": True}
            db.session.flush()
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        real(state, part, result)

    monkeypatch.setattr(compat_audio, "_record", busy_once)
    assert compat_audio.sweep() == 1
    db.session.expire_all()
    state = db.session.query(ChapterState).one()
    assert len(tries) == 2 and state.compat_audio["added"]


def test_waits_while_jellyfin_remakes_thumbnails(app, monkeypatch):
    from test_enrichment import _movie

    from curatarr import thumbnails
    from curatarr.models import MediaIdentity

    _movie("/movies/Loud (2020)/Loud.mkv")
    db.session.query(MediaIdentity).one().radarr_id = 1
    db.session.commit()
    monkeypatch.setattr("curatarr.services.client", lambda kind: Radarr())
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "playback_sessions", list)
    monkeypatch.setattr("curatarr.filecheck_jobs._dry_run", lambda media: False)
    monkeypatch.setattr(thumbnails, "room", lambda: False)
    monkeypatch.setattr(
        compat_audio, "_add", lambda *a: pytest.fail("rebuilt while Jellyfin is behind")
    )
    assert compat_audio.sweep() == 0


def test_an_idle_jellyfin_app_doesnt_count_as_playing(app, monkeypatch):
    """An episode not yet linked to Jellyfin has no item id, and an app that's
    open but playing nothing has no now-playing id: they used to match, so
    every unlinked episode looked like it was playing and was never done."""
    from test_enrichment import _movie

    from curatarr.models import MediaPart

    _movie("/movies/Loud (2020)/Loud.mkv")
    part = db.session.query(MediaPart).one()
    part.kind, part.jellyfin_id = "episode", None
    db.session.commit()
    sessions = [{"DeviceName": "Living room", "NowPlayingItem": None}]
    monkeypatch.setattr(enrichment, "playback_sessions", lambda: sessions)
    assert not compat_audio._playing(part)
    sessions.append(
        {"NowPlayingItem": {"Id": "x", "Path": "/Movies/Loud (2020)/Loud.mkv"}}
    )
    assert compat_audio._playing(part)  # matched by file name


@pytest.mark.skipif(not tool("ffmpeg"), reason="needs FFmpeg")
def test_browsers_get_a_stereo_aac_track_and_tvs_keep_theirs(tmp_path):
    """Added last and not default: TVs keep the surround track."""
    import subprocess

    from curatarr.mediafile import add_compatible_audio, browser_audio_source, probe

    ffmpeg = tool("ffmpeg")
    path = str(tmp_path / "eac3.mkv")
    subprocess.run(
        [
            ffmpeg,
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
            "-ac",
            "6",
            "-c:v",
            "mpeg4",
            "-c:a",
            "eac3",
            "-metadata:s:a:0",
            "language=eng",
            "-disposition:a:0",
            "default",
            path,
        ],
        check=True,
    )
    before = probe(path)
    source = browser_audio_source(before)
    assert source is not None
    after = add_compatible_audio(path, before, source, for_browsers=True)
    audio = [s for s in after.streams if s["codec_type"] == "audio"]
    assert [s["codec_name"] for s in audio] == ["eac3", "aac"]
    assert audio[0]["default"] and not audio[1]["default"]
    assert audio[1]["channels"] == 2 and audio[1]["language"] == "eng"
    assert browser_audio_source(after) is None  # not added twice
