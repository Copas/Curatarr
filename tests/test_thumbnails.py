"""File rebuilds wait for Jellyfin to remake scrubbing thumbnails."""

from datetime import timedelta

from curatarr import db, thumbnails
from curatarr.models import utcnow
from curatarr.services import set_setting, setting


class Part:
    def __init__(self, item):
        self.jellyfin_id, self.kind = item, "episode"


class Jellyfin:
    def __init__(self):
        self.with_thumbnails = set()
        self.asked = []

    def items_trickplay(self, ids):
        self.asked.append(list(ids))
        return [
            {
                "Id": i,
                "Trickplay": {i: {"320": {}}} if i in self.with_thumbnails else {},
            }
            for i in ids
        ]


def _jellyfin(monkeypatch):
    fake = Jellyfin()
    monkeypatch.setattr("curatarr.services.client", lambda kind: fake)
    return fake


def test_rebuilds_wait_while_three_files_await_thumbnails(app, monkeypatch):
    fake = _jellyfin(monkeypatch)
    for n in range(3):
        assert thumbnails.room()
        thumbnails.note_rebuilt(Part(f"item{n}"), None, had_thumbnails=True)
    assert not thumbnails.room()
    assert thumbnails.status() == {"waiting": 3, "paused": True}
    fake.with_thumbnails.add("item1")  # Jellyfin made one file's again
    assert thumbnails.room()
    assert [w["item"] for w in setting(thumbnails.KEY)] == ["item0", "item2"]


def test_files_without_thumbnails_before_dont_count(app, monkeypatch):
    _jellyfin(monkeypatch)
    for n in range(5):
        thumbnails.note_rebuilt(Part(f"item{n}"), None, had_thumbnails=False)
    assert thumbnails.room() and setting(thumbnails.KEY, []) == []


def test_a_file_waits_two_days_at_most(app, monkeypatch):
    _jellyfin(monkeypatch)
    old = (utcnow() - timedelta(days=3)).isoformat()
    set_setting(thumbnails.KEY, [{"item": f"i{n}", "at": old} for n in range(3)])
    db.session.commit()
    assert thumbnails.room()


def test_jellyfin_unreachable_waits(app, monkeypatch):
    from curatarr.integrations import IntegrationError

    class Down:
        def items_trickplay(self, ids):
            raise IntegrationError("down")

    monkeypatch.setattr("curatarr.services.client", lambda kind: Down())
    thumbnails.note_rebuilt(Part("item"), None, had_thumbnails=True)
    assert not thumbnails.room()


def test_can_be_turned_off(app, monkeypatch):
    monkeypatch.setattr(thumbnails, "enabled", lambda: False)
    set_setting(
        thumbnails.KEY,
        [{"item": f"i{n}", "at": utcnow().isoformat()} for n in range(9)],
    )
    db.session.commit()
    assert thumbnails.room()


def test_has_thumbnails_reads_dashed_ids(app, monkeypatch):
    fake = _jellyfin(monkeypatch)
    fake.with_thumbnails.add("ABC-DEF")
    assert thumbnails.has_thumbnails(Part("ABC-DEF"), None)
    assert not thumbnails.has_thumbnails(Part("other"), None)


def test_a_rebuild_while_editing_in_place_counts_too(app, monkeypatch):
    from test_enrichment import _movie

    from curatarr import mediafile
    from curatarr.models import MediaPart

    _movie("/movies/Loud (2020)/Loud.mkv")
    db.session.query(MediaPart).one().jellyfin_id = "loud"
    db.session.commit()
    fake = _jellyfin(monkeypatch)
    fake.with_thumbnails.add("loud")
    rebuilt = []
    monkeypatch.setattr(mediafile, "seek_index_hidden", lambda path: True)
    monkeypatch.setattr(mediafile, "rebuild_layout", rebuilt.append)

    class Done:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(mediafile, "_run", lambda args, timeout: Done())
    mediafile.set_default_audio(
        "/local/Loud.mkv",
        mediafile.MediaInfo(
            "/local/Loud.mkv",
            1,
            1.0,
            60.0,
            "matroska",
            streams=[{"index": 1, "codec_type": "audio"}],
        ),
        1,
    )
    assert rebuilt == ["/local/Loud.mkv"]
    assert [w["item"] for w in setting(thumbnails.KEY)] == ["loud"]


def test_the_seek_repair_leaves_the_last_spot_for_tv_friendly_audio(app, monkeypatch):
    from curatarr import compat_audio, seek_repair

    _jellyfin(monkeypatch)
    for n in range(2):
        thumbnails.note_rebuilt(Part(f"item{n}"), None, had_thumbnails=True)
    monkeypatch.setattr(compat_audio, "enabled", lambda: True)
    set_setting(compat_audio.REMAINING_KEY, {"films": 0, "episodes": 5})
    db.session.commit()
    assert thumbnails.room()  # audio may take the third spot
    assert not thumbnails.room(leave=seek_repair._audio_waiting())
    set_setting(compat_audio.REMAINING_KEY, {"films": 0, "episodes": 0})
    db.session.commit()
    assert thumbnails.room(leave=seek_repair._audio_waiting())
