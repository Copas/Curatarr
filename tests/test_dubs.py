import pytest

from curatarr import db, dub_jobs, dubs, enrichment
from curatarr.mediafile import MediaInfo
from curatarr.models import ChapterState
from curatarr.services import set_setting
from tests.test_enrichment import _movie

PATH = "/movies/Film (2020)/Film.mkv"


def test_language_codes():
    assert dubs.code("eng") == dubs.code("English") == dubs.code("en-US") == "en"
    assert dubs.code("fre") == dubs.code("fra") == dubs.code("French") == "fr"
    assert dubs.code("und") is None and dubs.code(None) is None
    assert dubs.name("da") == "Danish"


def test_a_track_needs_several_confident_agreeing_samples():
    assert dubs.heard([("en", 0.9), ("en", 0.8), ("en", 0.95), ("fr", 0.3)]) == "en"
    assert dubs.heard([("en", 0.9), ("en", 0.5), ("en", 0.4)]) is None  # too unsure
    assert dubs.heard([("en", 0.9), ("da", 0.9), ("nl", 0.9), ("en", 0.9)]) is None


def _streams(*tracks):
    streams = [{"index": 0, "codec_type": "video"}]
    for number, (language, default) in enumerate(tracks, start=1):
        streams.append(
            {
                "index": number,
                "codec_type": "audio",
                "language": language,
                "default": default,
            }
        )
    return streams


def test_plan_relabels_a_fake_english_track_and_finds_no_english():
    result = dubs.plan(_streams(("eng", True)), {1: "da"})
    assert result["verdict"] == "no_english"
    assert result["actions"] == [("label", 1, "da")]


def test_plan_makes_the_real_english_track_the_default():
    result = dubs.plan(_streams(("fre", True), ("eng", False)), {1: "fr", 2: "en"})
    assert result["verdict"] == "english" and result["actions"] == [("default", 2)]


def test_plan_leaves_foreign_labels_and_unclear_tracks_alone():
    result = dubs.plan(_streams(("fre", True), ("ger", False)), {1: "it", 2: None})
    assert result == {**result, "verdict": "unsure", "actions": []}


class FakeRadarr:
    def __init__(self, original="Danish"):
        self.original = original
        self.calls = []

    def movies(self):
        return [{"id": 7, "originalLanguage": {"id": 9, "name": self.original}}]

    def movie(self, movie_id):
        return {"id": movie_id, "movieFile": {"id": 55}}

    def grabs(self, movie_id):
        return [{"id": 300, "sourceTitle": "Film.2020.1080p.WEB-DL"}]

    def mark_failed(self, history_id):
        self.calls.append(("failed", history_id))

    def delete_movie_file(self, file_id):
        self.calls.append(("delete", file_id))

    def search_movie(self, movie_id):
        self.calls.append(("search", movie_id))


@pytest.fixture
def dub_env(app, monkeypatch):
    radarr = FakeRadarr()
    files = {"streams": _streams(("eng", True)), "edits": []}

    def probe(path):
        return MediaInfo(
            path=path,
            size=1000,
            mtime=1.0,
            duration=6000.0,
            format_name="matroska,webm",
            streams=[dict(s) for s in files["streams"]],
        )

    def relabel(path, info, index, three, short):
        files["edits"].append(("label", index, three))
        for s in files["streams"]:
            if s["index"] == index:
                s["language"] = three

    def make_default(path, info, index):
        files["edits"].append(("default", index))
        for s in files["streams"]:
            if s.get("codec_type") == "audio":
                s["default"] = s["index"] == index

    monkeypatch.setattr(dub_jobs, "client", lambda _kind: radarr)
    monkeypatch.setattr(dub_jobs, "_dry_run", lambda media: False)
    monkeypatch.setattr(dub_jobs, "probe", probe)
    monkeypatch.setattr(dub_jobs, "can_write", lambda info: (True, None))
    monkeypatch.setattr(dub_jobs, "set_audio_language", relabel)
    monkeypatch.setattr(dub_jobs, "set_default_audio", make_default)
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(enrichment, "_refresh_jellyfin", lambda part, media: None)
    monkeypatch.setattr(enrichment, "_language_model", lambda config, opts: None)
    files["heard"] = {}
    monkeypatch.setattr(
        dubs,
        "listen",
        lambda info, tracks, device, model, make_room=None: {
            t: [(files["heard"][t], 0.9)] * 5 for t in tracks
        },
    )
    return radarr, files


def test_films_made_in_english_are_only_marked_checked(dub_env):
    radarr, files = dub_env
    radarr.original = "English"
    _library, _media, _part = _movie(PATH)
    assert dub_jobs.sweep() == {"english": 1, "foreign": 0}
    state = db.session.query(ChapterState).one()
    assert state.dub_result == {"original": "en"} and not files["edits"]
    assert dub_jobs.sweep() == {"english": 0, "foreign": 0}  # not again


def test_a_fake_english_dub_is_relabelled_listed_and_replaced(app, client, dub_env):
    radarr, files = dub_env
    files["heard"] = {1: "da"}
    _library, _media, _part = _movie(PATH)
    assert dub_jobs.sweep() == {"english": 0, "foreign": 1}
    assert files["edits"] == [("label", 1, "dan")]
    state = db.session.query(ChapterState).one()
    assert state.dub_result["verdict"] == "no_english"
    assert dub_jobs.review()["to_act"] == [state]
    page = client.get("/chapters/dubs").get_data(as_text=True)
    assert "none of its audio sounds English" in page and "Danish" in page
    assert radarr.calls == []  # replacing waits for approval by default
    client.post(f"/chapters/dubs/{state.id}/replace")
    assert radarr.calls == [("failed", 300), ("delete", 55), ("search", 7)]
    assert dub_jobs.review()["to_act"] == []


def test_replace_automatically_when_switched_on(dub_env):
    radarr, files = dub_env
    files["heard"] = {1: "da"}
    _library, _media, _part = _movie(PATH)
    opts = enrichment.options()
    set_setting("chapter_options", {**opts, "dub_replace_automatically": True})
    dub_jobs.sweep()
    assert ("search", 7) in radarr.calls


def test_dry_run_keeps_the_file(dub_env, monkeypatch):
    radarr, files = dub_env
    files["heard"] = {1: "da"}
    _movie(PATH)
    dub_jobs.sweep()
    monkeypatch.setattr(dub_jobs, "_dry_run", lambda media: True)
    with pytest.raises(ValueError, match="dry run"):
        dub_jobs.replace(db.session.query(ChapterState).one())
    assert radarr.calls == []


def test_the_page_shows_what_a_label_was(client, dub_env):
    _radarr, files = dub_env
    files["heard"] = {1: "da"}
    _movie(PATH)
    dub_jobs.sweep()
    page = client.get("/chapters/dubs").get_data(as_text=True)
    assert "labelled <strong>Danish</strong> (was English)" in page


def test_a_real_english_track_becomes_the_default(dub_env):
    radarr, files = dub_env
    radarr.original = "French"
    files["streams"] = _streams(("fre", True), ("eng", False))
    files["heard"] = {1: "fr", 2: "en"}
    _library, _media, _part = _movie(PATH)
    dub_jobs.sweep()
    assert files["edits"] == [("default", 2)]
    assert dub_jobs.review()["fixed"] == [db.session.query(ChapterState).one()]


def test_listening_waits_while_playback_has_priority(dub_env):
    _radarr, files = dub_env
    files["heard"] = {1: "da"}
    _movie(PATH)
    set_setting("chapters_paused_since", "2026-01-01T00:00:00+00:00")
    assert dub_jobs.sweep() == {"english": 0, "foreign": 0}
    set_setting("chapters_paused_since", None)
    assert dub_jobs.sweep()["foreign"] == 1


def test_a_replaced_file_is_checked_again(dub_env):
    _radarr, files = dub_env
    files["heard"] = {1: "da"}
    _library, _media, part = _movie(PATH)
    dub_jobs.sweep()
    part.size_bytes = 5000  # Radarr imported a different file
    db.session.commit()
    files["streams"], files["heard"] = _streams(("eng", True)), {1: "en"}
    dub_jobs.sweep()
    assert db.session.query(ChapterState).one().dub_result["verdict"] == "english"


def test_an_unclear_default_track_is_not_swapped_for_another_english_one():
    # The second English track could be a commentary.
    result = dubs.plan(_streams(("eng", True), ("eng", False)), {1: None, 2: "en"})
    assert result["verdict"] == "english" and result["actions"] == []


def test_a_foreign_films_chapter_job_waits_for_its_dub_check(dub_env):
    _radarr, files = dub_env
    files["heard"] = {1: "da"}
    _library, _media, part = _movie(PATH)
    state = enrichment.request(part, "generate")
    assert dub_jobs.holds_chapters(state, True)
    assert not dub_jobs.holds_chapters(state, False)  # check switched off
    dub_jobs.sweep()
    assert not dub_jobs.holds_chapters(db.session.get(ChapterState, state.id), True)
