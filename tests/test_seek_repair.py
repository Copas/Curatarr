"""Files whose seek index Roku players can't reach: never made, and repaired."""

import subprocess

import pytest

from curatarr import db, enrichment, mediafile, seek_repair
from curatarr.chapters import Chapter
from curatarr.mediafile import seek_index_hidden, tool
from curatarr.models import ChapterState, LifecycleAction

TOOLS = all(tool(name) for name in ("ffmpeg", "mkvmerge", "mkvpropedit", "ffprobe"))


def _mkv(tmp_path):
    source, path = str(tmp_path / "a.mp4"), str(tmp_path / "Show - S01E01.mkv")
    subprocess.run(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine",
            "-t",
            "30",
            "-c:v",
            "libx264",
            "-g",
            "48",
            "-c:a",
            "aac",
            source,
        ],
        check=True,
    )
    subprocess.run([tool("mkvmerge"), "-q", "-o", path, source], check=True)
    return path


def _hide_index(path):
    """What mkvpropedit does when chapters don't fit at the start."""
    chapters = str(path) + ".xml"
    with open(chapters, "w") as handle:
        handle.write(
            '<?xml version="1.0"?><Chapters><EditionEntry>'
            + "".join(
                f"<ChapterAtom><ChapterTimeStart>00:00:{n:02}.000</ChapterTimeStart>"
                f"<ChapterDisplay><ChapterString>Part {n}</ChapterString>"
                "</ChapterDisplay></ChapterAtom>"
                for n in range(0, 30, 5)
            )
            + "</EditionEntry></Chapters>"
        )
    subprocess.run(
        [tool("mkvpropedit"), "-q", path, "--chapters", chapters], check=True
    )


@pytest.mark.skipif(not TOOLS, reason="needs FFmpeg and MKVToolNix")
def test_writing_chapters_never_leaves_the_index_out_of_reach(tmp_path):
    path = _mkv(tmp_path)
    assert not seek_index_hidden(path)
    other = str(tmp_path / "copy.mkv")
    subprocess.run(["cp", path, other], check=True)
    _hide_index(other)
    assert seek_index_hidden(other)  # the problem, as mkvpropedit leaves it
    mediafile.write_chapters(
        path, [Chapter(float(n), f"Part {n}") for n in range(0, 30, 5)]
    )
    assert not seek_index_hidden(path)
    info = mediafile.probe(path)
    assert len(info.chapters) == 6
    assert not list(tmp_path.glob(".*partial"))


@pytest.mark.skipif(not TOOLS, reason="needs FFmpeg and MKVToolNix")
def test_rebuilding_keeps_everything(tmp_path):
    path = _mkv(tmp_path)
    _hide_index(path)
    before = mediafile.probe(path)
    after = mediafile.rebuild_layout(path)
    assert not seek_index_hidden(path)
    assert [s["codec_name"] for s in after.streams] == [
        s["codec_name"] for s in before.streams
    ]
    assert after.chapters == before.chapters
    assert abs(after.duration - before.duration) < 0.1


@pytest.mark.skipif(not TOOLS, reason="needs FFmpeg and MKVToolNix")
def test_a_file_with_its_audio_first_is_rebuilt_as_it_is(tmp_path):
    """Some web releases put the audio track before the video. mkvmerge put
    the video first, the check saw different tracks and kept the broken
    original: 201 files, Roku still unable to seek in them."""
    plain = _mkv(tmp_path)
    path = str(tmp_path / "Top Gear - S01E01.mkv")
    subprocess.run(
        [tool("mkvmerge"), "-q", "--track-order", "0:1,0:0", "-o", path, plain],
        check=True,
    )
    _hide_index(path)
    before = mediafile.probe(path)
    assert [s["codec_type"] for s in before.streams] == ["audio", "video"]
    after = mediafile.rebuild_layout(path)
    assert [s["codec_type"] for s in after.streams] == ["audio", "video"]
    assert not seek_index_hidden(path)


def test_not_matroska_or_unreadable_is_left_alone(tmp_path):
    other = tmp_path / "film.mp4"
    other.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    assert not seek_index_hidden(str(other))
    assert not seek_index_hidden(str(tmp_path / "missing.mkv"))


def test_the_sweep_rebuilds_hidden_files_once_and_checks_the_rest(app, monkeypatch):
    from test_enrichment import _movie

    from curatarr.models import MediaPart

    _movie("/movies/Loud (2020)/Loud.mkv")
    db.session.query(MediaPart).one().size_bytes = 100
    db.session.commit()
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "playback_sessions", list)
    monkeypatch.setattr(enrichment, "_refresh_jellyfin", lambda part, media: None)

    class Radarr:
        def items_trickplay(self, ids):
            return []

        def rescan_movie(self, movie_id):
            pass

    monkeypatch.setattr("curatarr.services.client", lambda kind: Radarr())
    hidden = {"yes": True}
    monkeypatch.setattr(seek_repair, "seek_index_hidden", lambda p: hidden["yes"])
    monkeypatch.setattr("curatarr.mediafile.file_identity_of", lambda p: (100, 1.0))

    def rebuild(path):
        hidden["yes"] = False
        return mediafile.MediaInfo(path, 120, 2.0, 60.0, "matroska,webm")

    monkeypatch.setattr(seek_repair, "rebuild_layout", rebuild)
    # Not automatic (a new installation): found and listed, nothing rebuilt.
    assert seek_repair.sweep() == 0
    (found,) = seek_repair.found_rows()
    assert seek_repair.progress()["found"] == 1
    assert "Roku players can" in app.test_client().get("/chapters/problems").text
    app.test_client().post(f"/chapters/problems/seek/{found.id}/fix")
    assert seek_repair.sweep() == 1
    assert seek_repair.found_rows() == []
    state = db.session.query(ChapterState).one()
    assert state.seek_layout["rebuilt"] and state.seek_layout_size == 120
    assert db.session.query(MediaPart).one().size_bytes == 120
    assert (
        db.session.query(LifecycleAction)
        .filter_by(action_type="seek_index_repaired")
        .count()
        == 1
    )
    assert seek_repair.sweep() == 0  # checked; not again unless the file changes
    assert seek_repair.progress()["rebuilt"] == 1


def test_a_playing_file_waits(app, monkeypatch):
    from test_enrichment import _movie

    from curatarr.models import MediaPart

    _movie("/movies/Loud (2020)/Loud.mkv")
    db.session.query(MediaPart).one().size_bytes = 100
    db.session.commit()
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(seek_repair, "seek_index_hidden", lambda p: True)
    monkeypatch.setattr(seek_repair, "_playing", lambda part: True)
    monkeypatch.setattr(
        seek_repair, "rebuild_layout", lambda p: pytest.fail("rebuilt while playing")
    )
    from curatarr.services import set_setting

    set_setting("auto_fixes", {"seek": True})  # automatic: it would rebuild, but waits
    assert seek_repair.sweep() == 0
    assert db.session.query(ChapterState).one().seek_layout is None


def test_left_alone_once_told_to(app, monkeypatch):
    from test_enrichment import _movie

    from curatarr.models import MediaPart

    _movie("/movies/Loud (2020)/Loud.mkv")
    db.session.query(MediaPart).one().size_bytes = 100
    db.session.commit()
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(seek_repair, "seek_index_hidden", lambda p: True)
    monkeypatch.setattr(seek_repair, "rebuild_layout", lambda p: pytest.fail("rebuilt"))
    seek_repair.sweep()
    (found,) = seek_repair.found_rows()
    app.test_client().post(f"/chapters/problems/seek/{found.id}/dismiss")
    assert seek_repair.found_rows() == []
    assert seek_repair.sweep() == 0


def test_on_by_default(app):
    assert seek_repair.enabled()
