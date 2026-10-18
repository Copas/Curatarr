from typing import ClassVar

import pytest
from conftest import database_free
from test_enrichment import _movie

from curatarr import better_copy, db, dub_jobs, dubs, enrichment
from curatarr.mediafile import MediaInfo
from curatarr.models import ChapterState
from curatarr.services import set_setting

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

    audio = ""

    def movies(self):
        return [
            {
                "id": 7,
                "originalLanguage": {"id": 9, "name": self.original},
                "movieFile": {"mediaInfo": {"audioLanguages": self.audio}},
            }
        ]

    def items_trickplay(self, ids):
        return []

    def rescan_movie(self, movie_id):
        self.calls.append(("rescan", movie_id))

    def movie(self, movie_id):
        return {"id": movie_id, "movieFile": {"id": 55}}

    def grabs(self, movie_id):
        return [{"id": 300, "sourceTitle": "Film.2020.1080p.WEB-DL"}]

    available: ClassVar[list] = [
        {"title": "Film.2020.1080p.BluRay.English-DUB", "rejections": []}
    ]

    def releases(self, _movie_id):
        return self.available

    def mark_failed(self, history_id):
        self.calls.append(("failed", history_id))

    def delete_movie_file(self, file_id):
        self.calls.append(("delete", file_id))

    def set_monitored(self, movie_id, monitored):
        self.calls.append(("monitored", movie_id, monitored))

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
    monkeypatch.setattr("curatarr.services.client", lambda _kind: radarr)
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
    better_copy.sweep()  # a copy Radarr would take exists
    assert radarr.calls == [
        ("failed", 300),
        ("monitored", 7, True),  # so a failed download is tried again
        ("delete", 55),
        ("search", 7),
    ]
    assert dub_jobs.review()["to_act"] == []


def test_replace_automatically_when_switched_on(dub_env):
    radarr, files = dub_env
    files["heard"] = {1: "da"}
    _library, _media, _part = _movie(PATH)
    opts = enrichment.options()
    set_setting("chapter_options", {**opts, "dub_replace_automatically": True})
    dub_jobs.sweep()
    better_copy.sweep()
    assert ("search", 7) in radarr.calls


def test_a_film_with_no_english_copy_anywhere_is_kept(client, dub_env):
    """Amélie and Pluto were deleted with nothing to replace them."""
    radarr, files = dub_env
    files["heard"] = {1: "da"}
    radarr.available = [
        {"title": "Film.2020.Danish.1080p", "rejections": ["Language: Not English"]}
    ]
    _movie(PATH)
    dub_jobs.sweep()
    state = db.session.query(ChapterState).one()
    client.post(f"/chapters/dubs/{state.id}/replace")
    better_copy.sweep()
    assert radarr.calls == []
    assert "No better copy was available" in client.get("/chapters/dubs").get_data(
        as_text=True
    )


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


def test_removable_keeps_english_and_unclear_tracks():
    streams = _streams(("eng", True), ("ita", False), ("tur", False)) + [
        {"index": 4, "codec_type": "subtitle", "language": "ita"},
        {"index": 5, "codec_type": "subtitle", "language": "eng"},
        {"index": 6, "codec_type": "subtitle", "language": None},
    ]
    result = dubs.plan(streams, {1: "en", 2: "it", 3: None})
    assert dubs.removable(result, streams) == {"audio": [2], "subtitles": [4]}
    unsure = dubs.plan(streams, {1: None, 2: "it", 3: None})
    assert dubs.removable(unsure, streams) == {"audio": [], "subtitles": []}


def test_other_languages_are_removed_only_when_switched_on(app, dub_env, monkeypatch):
    radarr, files = dub_env
    radarr.original = "English"
    radarr.audio = "eng/ita"
    files["streams"] = _streams(("eng", True), ("ita", False))
    files["heard"] = {1: "en", 2: "it"}
    removed = []

    def remove(path, info, drop, default):
        # Minutes of copying: nothing may be left unsaved (it holds SQLite's lock).
        assert database_free(app)
        removed.append((sorted(drop), default))
        return MediaInfo(
            path=path, size=800, mtime=2.0, duration=6000.0, format_name="matroska,webm"
        )

    monkeypatch.setattr(dub_jobs, "remove_tracks", remove)
    _movie(PATH)
    dub_jobs.sweep()  # an English film with an Italian dub is listened to
    assert db.session.query(ChapterState).one().dub_result["verdict"] == "english"
    assert removed == []  # off by default
    set_setting(
        "chapter_options", {**enrichment.options(), "dub_remove_other_languages": True}
    )
    dub_jobs.recheck(db.session.query(ChapterState).one())
    dub_jobs.sweep()
    state = db.session.query(ChapterState).one()
    assert removed == [([2], 1)]
    assert state.dub_result["removed"]["audio"] == [[2, "it"]]
    assert state.dub_file_size == 800
    assert dub_jobs.review()["fixed"] == [state]


def test_english_films_with_only_english_audio_are_not_listened_to(
    dub_env, monkeypatch
):
    radarr, _files = dub_env
    radarr.original, radarr.audio = "English", "eng"
    monkeypatch.setattr(dubs, "listen", lambda *a, **k: pytest.fail("listened"))
    _movie(PATH)
    assert dub_jobs.sweep() == {"english": 1, "foreign": 0}


def test_remove_tracks_rewrites_a_real_file(tmp_path):
    import shutil
    import subprocess

    from curatarr import mediafile

    ffmpeg, merge = mediafile.tool("ffmpeg"), mediafile.tool("mkvmerge")
    if not ffmpeg or not merge or not mediafile.tool("ffprobe"):
        pytest.skip("needs ffmpeg, ffprobe and mkvmerge")
    parts = []
    for name, freq in (("v", None), ("a1", 440), ("a2", 550), ("a3", 660)):
        out = tmp_path / f"{name}.mkv"
        source = (
            ["-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=6"]
            if freq is None
            else ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration=6"]
        )
        subprocess.run([ffmpeg, "-v", "error", "-y", *source, str(out)], check=True)
        parts.append(out)
    for lang in ("ita", "eng"):
        (tmp_path / f"{lang}.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    (tmp_path / "chapters.txt").write_text(
        "CHAPTER01=00:00:00.000\nCHAPTER01NAME=One\nCHAPTER02=00:00:03.000\nCHAPTER02NAME=Two\n"
    )
    film = tmp_path / "film.mkv"
    subprocess.run(
        [
            merge,
            "-q",
            "-o",
            str(film),
            "--chapters",
            str(tmp_path / "chapters.txt"),
            str(parts[0]),
            "--language",
            "0:ita",
            "--default-track-flag",
            "0:1",
            str(parts[1]),
            "--language",
            "0:eng",
            "--default-track-flag",
            "0:0",
            str(parts[2]),
            "--language",
            "0:tur",
            "--default-track-flag",
            "0:0",
            str(parts[3]),
            "--language",
            "0:ita",
            str(tmp_path / "ita.srt"),
            "--language",
            "0:eng",
            str(tmp_path / "eng.srt"),
        ],
        check=True,
    )
    info = mediafile.probe(str(film))
    audio = [s for s in info.streams if s["codec_type"] == "audio"]
    subs = [s for s in info.streams if s["codec_type"] == "subtitle"]
    english = next(s["index"] for s in audio if s["language"] == "eng")
    drop = [s["index"] for s in audio if s["language"] != "eng"]
    drop += [s["index"] for s in subs if s["language"] == "ita"]
    after = mediafile.remove_tracks(str(film), info, drop, english)
    kept = [(s["codec_type"], s["language"], s["default"]) for s in after.streams]
    assert ("audio", "eng", True) in kept
    assert [k for k in kept if k[0] == "audio"] == [("audio", "eng", True)]
    assert [k[1] for k in kept if k[0] == "subtitle"] == ["eng"]
    assert len(after.chapters) == 2 and abs(after.duration - info.duration) < 1
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []
    shutil.rmtree(tmp_path, ignore_errors=True)
