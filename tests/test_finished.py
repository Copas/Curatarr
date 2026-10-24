"""Something stopped in its credits counts as watched."""

from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import db, finished
from curatarr.models import ChapterState, LifecycleAction

MIN = 60 * 10_000_000  # Jellyfin ticks in a minute


class FakeJellyfin:
    def __init__(self, items, segments=None, playing=()):
        self.items, self.segments, self.playing = items, segments or {}, playing
        self.played = []

    def users(self):
        return [{"Id": "u1", "Name": "Viewer"}]

    def sessions(self):
        return [
            {"UserId": "u1", "NowPlayingItem": {"Id": item}} for item in self.playing
        ]

    def resume(self, user_id, fields=None):
        return self.items

    def media_segments(self, item_id):
        return self.segments.get(item_id, [])

    def mark_played(self, user_id, item_id):
        self.played.append((user_id, item_id))


def _stopped(item_id, minutes, runtime, kind="Movie", path="/Movies/Film.mkv"):
    return {
        "Id": item_id,
        "Type": kind,
        "Name": "Film",
        "Path": path,
        "RunTimeTicks": runtime * MIN,
        "UserData": {"PlaybackPositionTicks": minutes * MIN},
    }


def test_the_rule():
    reason = finished.finished_reason
    # In the credits (or within half a minute of them).
    assert reason(78.2 * 60, 84 * 60, 78.3 * 60, None, False) == "in the credits"
    # After the last line, with a film's ending and credits left: Wrong Turn.
    assert reason(74.8 * 60, 84 * 60, 78.3 * 60, 67.2 * 60, False)
    # Dialogue still to come: Wrong Turn 2, stopped 17 minutes before the end.
    assert not reason(77 * 60, 97 * 60, 94 * 60, 92.3 * 60, False)
    # Silent, but too much left to be the ending.
    assert not reason(60 * 60, 90 * 60, None, 55 * 60, False)
    # Episodes have short credits: three minutes, not twelve.
    assert not reason(36 * 60, 44 * 60, None, 35 * 60, True)
    assert reason(42 * 60, 44 * 60, None, 41 * 60, True)
    # Nothing to go on: Jellyfin's own 90% decides.
    assert not reason(80 * 60, 84 * 60, None, None, False)


def test_an_opening_credits_marker_doesnt_count():
    segments = [{"Type": "Outro", "StartTicks": 2 * MIN}]
    assert finished.credits_start(segments, 90 * 60) is None
    segments.append({"Type": "Outro", "StartTicks": 85 * MIN})
    assert finished.credits_start(segments, 90 * 60) == 85 * 60


def test_partial_subtitles_dont_say_where_dialogue_ends(app, tmp_path):
    full, partial = tmp_path / "full.srt", tmp_path / "forced.srt"
    full.write_text(
        "1\n00:01:00,000 --> 00:01:02,000\nHi\n\n2\n01:07:09,200 --> 01:07:11,200\nBye\n"
    )
    partial.write_text("1\n00:20:00,000 --> 00:20:02,000\nGracias\n")
    assert finished.last_dialogue(full) == 67 * 60 + 11.2

    class Part:
        file_path = None

    class State:
        subtitle_path = str(partial)

    assert finished.dialogue_ends(Part(), State(), 84 * 60) is None
    State.subtitle_path = str(full)
    assert finished.dialogue_ends(Part(), State(), 84 * 60) == 67 * 60 + 11.2


def test_a_film_stopped_in_its_credits_is_marked_watched_once(
    app,
    media_env,  # noqa: F811
    monkeypatch,
):
    _library, media, _part = _movie("/movies/Film (2003)/Film.mkv")
    media.jellyfin_id = "f" * 32
    db.session.commit()
    jellyfin = FakeJellyfin(
        [
            _stopped("f" * 32, 79, 84, path="/Movies/Film (2003)/Film.mkv"),
            _stopped("a" * 32, 40, 84),  # halfway: Jellyfin's to keep
        ],
        segments={"f" * 32: [{"Type": "Outro", "StartTicks": 78 * MIN}]},
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    assert finished.sweep() == 1
    assert jellyfin.played == [("u1", "f" * 32)]
    action = (
        db.session.query(LifecycleAction)
        .filter_by(action_type="finished_at_credits")
        .one()
    )
    assert "Viewer stopped" in action.reason_text
    assert "in the credits" in action.reason_text
    assert finished.sweep() == 0  # looked at again only every few minutes


def test_something_still_playing_is_left_alone(app, monkeypatch):
    jellyfin = FakeJellyfin(
        [_stopped("f" * 32, 80, 84)],
        segments={"f" * 32: [{"Type": "Outro", "StartTicks": 78 * MIN}]},
        playing=["f" * 32],
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    assert finished.sweep() == 0 and jellyfin.played == []


def test_after_the_last_line_uses_the_files_subtitles(
    app,
    media_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    _library, media, part = _movie("/movies/Film (2003)/Film.mkv")
    media.jellyfin_id = "f" * 32
    subtitles = tmp_path / "Film.en.Curatarr.srt"
    subtitles.write_text("1\n01:07:09,200 --> 01:07:11,200\nBye\n")
    db.session.add(
        ChapterState(
            media_part_id=part.id,
            media_identity_id=media.id,
            subtitle_path=str(subtitles),
        )
    )
    db.session.commit()
    jellyfin = FakeJellyfin(
        [_stopped("f" * 32, 75, 84, path="/Movies/Film (2003)/Film.mkv")]
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    assert finished.sweep() == 1


def test_off_means_off(app, monkeypatch):
    monkeypatch.setattr(finished, "enabled", lambda: False)
    monkeypatch.setattr(
        "curatarr.services.client", lambda _kind: (_ for _ in ()).throw(AssertionError)
    )
    assert finished.sweep() == 0
