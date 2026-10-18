import subprocess
from types import SimpleNamespace
from typing import ClassVar

import pytest
from conftest import database_free
from PIL import Image, ImageDraw, ImageFont
from test_enrichment import _movie

from curatarr import better_copy, db, enrichment, filecheck, filecheck_jobs
from curatarr.mediafile import MediaInfo, tool
from curatarr.models import ChapterState, MediaIdentity, MediaPart
from curatarr.services import set_setting, setting

PATH = "/movies/Film (2020)/Film.mkv"


def frame(text=None, *, at="centre", tall=False):
    """A dark 640x360 frame, with outlined subtitle-like text if asked."""
    image = Image.new("L", (640, 360), 40)
    draw = ImageDraw.Draw(image)
    if tall:  # a bright, outlined object filling the bottom of the picture
        draw.rectangle((180, 230, 460, 355), fill=230, outline=0, width=4)
        for y in range(240, 350, 8):
            draw.line((190, y, 450, y), fill=10, width=2)
    if text:
        font = ImageFont.load_default(size=20)
        width = draw.textlength(text, font=font)
        x = {"centre": (640 - width) / 2, "left": 70}[at]
        draw.text((x, 315), text, fill=255, font=font, stroke_width=2, stroke_fill=0)
    return image


def test_a_centred_outlined_line_reads_as_subtitles():
    assert filecheck.subtitled(frame("I never said that, Mother."))
    assert not filecheck.subtitled(frame())
    assert not filecheck.subtitled(frame(tall=True))  # too tall for a text line
    assert not filecheck.subtitled(frame("Name, Job", at="left"))  # a caption
    assert not filecheck.subtitled(None)


def segments(count=40, every=60.0):
    return [
        SimpleNamespace(
            start=300 + i * every, end=303 + i * every, text="Something said here."
        )
        for i in range(count)
    ]


def test_subtitles_on_speech_but_not_silence_are_burned_in(monkeypatch):
    talking = {(s.start + s.end) / 2 for s in segments()}
    monkeypatch.setattr(
        filecheck,
        "grab",
        lambda path, t, width=640: frame("Hello there.") if t in talking else frame(),
    )
    result = filecheck.burned_subtitles("film.mkv", 3600, segments())
    assert result["verdict"] == "burned" and result["method"] == "speech"
    assert result["hits"] == result["samples"] and result["quiet_hits"] == 0


def test_text_on_screen_regardless_of_speech_is_not_subtitles(monkeypatch):
    monkeypatch.setattr(
        filecheck, "grab", lambda path, t, width=640: frame("SCORE 3-1")
    )
    result = filecheck.burned_subtitles("film.mkv", 3600, segments())
    assert result["verdict"] != "burned"  # on during the silences too


def test_without_a_transcript_frames_are_spread_evenly(monkeypatch):
    monkeypatch.setattr(filecheck, "grab", lambda path, t, width=640: frame())
    result = filecheck.burned_subtitles("film.mkv", 3600, [])
    assert (result["method"], result["verdict"], result["samples"]) == (
        "even",
        "clean",
        24,
    )


def test_health_finds_short_cut_off_quiet_and_damaged_files(monkeypatch):
    monkeypatch.setattr(
        filecheck, "grab", lambda path, t, width=640: frame() if t < 4000 else None
    )
    monkeypatch.setattr(filecheck, "_decode_errors", lambda path, t: False)
    early_talk = segments(25, 40)  # all said in the first ~20 minutes
    issues = filecheck.health("film.mkv", 6000, 140, early_talk, film=True)
    assert [i["kind"] for i in issues] == ["short", "truncated", "silent_end"]
    monkeypatch.setattr(filecheck, "grab", lambda path, t, width=640: frame())
    monkeypatch.setattr(filecheck, "_decode_errors", lambda path, t: t > 3000)
    issues = filecheck.health("film.mkv", 6000, 100, segments(80), film=True)
    assert [i["kind"] for i in issues] == ["decode_errors"]
    assert filecheck.health("film.mkv", 6000, 100, [], film=False) == [
        i for i in issues
    ]


class FakeArr:
    def __init__(self):
        self.calls = []

    def movies(self):
        return [{"id": 7, "runtime": 100}]

    def series(self):
        return [{"id": 3, "runtime": 22}]

    def movie(self, movie_id):
        return {"id": movie_id, "movieFile": {"id": 55}}

    def grabs(self, movie_id):
        return [{"id": 300, "sourceTitle": "Film.2020.1080p.HC.WEBRip"}]

    def episode_grabs(self, episode_id):
        return [{"id": 400, "sourceTitle": "Show.S01E01.HDTV"}]

    # What the indexers have: one whole, acceptable copy unless a test says otherwise.
    available: ClassVar[list] = [
        {"title": "Film.2020.1080p.BluRay-GOOD", "rejections": []}
    ]

    def releases(self, _item_id):
        return self.available

    def ignore_for_movie(self, movie_id, label, pattern, name):
        self.calls.append(("ignore", movie_id, label, name))

    def ignore_for_series(self, series_id, label, pattern, name):
        self.calls.append(("ignore", series_id, label, name))

    def mark_failed(self, history_id):
        self.calls.append(("failed", history_id))

    def delete_movie_file(self, file_id):
        self.calls.append(("delete", file_id))

    def delete_episode_file(self, file_id):
        self.calls.append(("delete episode", file_id))

    def set_monitored(self, movie_id, monitored):
        self.calls.append(("monitored", movie_id, monitored))

    def search_movie(self, movie_id):
        self.calls.append(("search", movie_id))

    def episode_search(self, ids):
        self.calls.append(("search episodes", list(ids)))


@pytest.fixture
def check_env(app, monkeypatch):
    arr = FakeArr()
    found = {"subtitles": "burned", "issues": []}
    monkeypatch.setattr(filecheck_jobs, "client", lambda _kind: arr)
    monkeypatch.setattr("curatarr.services.client", lambda _kind: arr)
    monkeypatch.setattr(filecheck_jobs, "_dry_run", lambda media: False)
    monkeypatch.setattr(
        filecheck_jobs,
        "probe",
        lambda path: MediaInfo(
            path=path, size=1000, mtime=1.0, duration=6000.0, format_name="matroska"
        ),
    )
    monkeypatch.setattr(enrichment, "file_exists", lambda p: True)
    monkeypatch.setattr(
        filecheck,
        "burned_subtitles",
        lambda path, duration, segments=None: {
            "verdict": found["subtitles"],
            "hits": 12,
            "samples": 14,
        },
    )
    monkeypatch.setattr(
        filecheck, "health", lambda *args, **kwargs: list(found["issues"])
    )
    return arr, found


def test_burned_in_subtitles_are_listed_and_replaced(client, check_env):
    arr, _found = check_env
    _library, _media, part = _movie(PATH)
    part.size_bytes = 1000
    db.session.commit()
    assert filecheck_jobs.sweep() == 1
    state = db.session.query(ChapterState).one()
    assert state.file_problem and filecheck_jobs.review()["to_act"] == [state]
    page = client.get("/chapters/problems").text
    assert "Subtitles are burned into the picture (on 12 of 14 frames checked)" in page
    assert "burned into the picture" in client.get("/").text  # Overview: needs you
    assert arr.calls == []  # replacing waits for approval by default
    client.post(f"/chapters/problems/{state.id}/replace")
    assert arr.calls == []  # it looks for a better copy first, in the worker
    assert better_copy.sweep() == 1
    assert arr.calls == [
        ("failed", 300),
        ("monitored", 7, True),  # so a failed download is tried again
        ("delete", 55),
        ("search", 7),
    ]
    assert filecheck_jobs.counts() == {"problems_to_act": 0, "files_checked": 1}
    assert filecheck_jobs.sweep() == 0  # not checked again until it's replaced
    part.size_bytes = 2000
    db.session.commit()
    assert filecheck_jobs.sweep() == 1


def test_a_clean_file_is_marked_checked_and_keep_hides_a_problem(client, check_env):
    _arr, found = check_env
    found["subtitles"] = "clean"
    _movie(PATH)
    filecheck_jobs.sweep()
    state = db.session.query(ChapterState).one()
    assert (
        state.file_problem is False
        and state.file_check["subtitles"]["verdict"] == "clean"
    )
    found["issues"] = [
        {"kind": "truncated", "text": "The picture stops before 1:20:00"}
    ]
    filecheck_jobs.recheck(state)
    filecheck_jobs.sweep()
    assert filecheck_jobs.review()["to_act"] == [state]
    client.post(f"/chapters/problems/{state.id}/dismiss")
    assert filecheck_jobs.review()["to_act"] == []


def test_a_broken_episode_is_replaced_through_sonarr(check_env):
    arr, found = check_env
    found["issues"] = [
        {"kind": "decode_errors", "text": "Picture or sound errors at 5:00, 9:00"}
    ]
    library, _media, _part = _movie(PATH)
    library.media_type = "tv"
    show = MediaIdentity(
        library_id=library.id, media_type="series", title="Show", sonarr_id=3
    )
    db.session.add(show)
    db.session.flush()
    episode = MediaPart(
        media_identity_id=show.id,
        kind="episode",
        season_number=1,
        episode_number=1,
        has_file=True,
        file_path="/tv/Show/S01E01.mkv",
        sonarr_episode_id=81,
        arr_file_id=82,
    )
    db.session.add(episode)
    db.session.commit()
    filecheck_jobs.sweep()
    state = db.session.query(ChapterState).filter_by(media_part_id=episode.id).one()
    assert "subtitles" not in state.file_check  # burned-in subtitles: films only
    filecheck_jobs.replace(state)
    assert arr.calls == [
        ("failed", 400),
        ("delete episode", 82),
        ("search episodes", [81]),
    ]


def test_problems_are_replaced_automatically_when_switched_on(check_env):
    arr, _found = check_env
    _movie(PATH)
    set_setting(
        "chapter_options", {**enrichment.options(), "file_replace_automatically": True}
    )
    filecheck_jobs.sweep()
    assert arr.calls == []  # only once a better copy is confirmed
    better_copy.sweep()
    assert ("search", 7) in arr.calls


def test_nothing_is_deleted_when_no_better_copy_exists(client, check_env):
    """The Millennium films: every whole English copy was rejected, so Replace
    would have left no copy at all."""
    arr, _found = check_env
    arr.available = [
        {"title": "Film.2020.Swedish.1080p", "rejections": ["Language: Not English"]},
        {"title": "Film.2020.1080p.REMUX", "rejections": ["Remux is not wanted"]},
        {
            "title": "Film.2020.480p",
            "rejections": [
                "Existing file meets cutoff",
                "SDTV is not wanted in profile",
            ],
        },
    ]
    _movie(PATH)
    filecheck_jobs.sweep()
    state = db.session.query(ChapterState).one()
    client.post(f"/chapters/problems/{state.id}/replace")
    better_copy.sweep()
    assert arr.calls[:1] == []  # nothing blocklisted, deleted or searched
    assert not [c for c in arr.calls if c[0] in ("failed", "delete")]
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.file_check["no_better"]["checked"] == 3
    page = client.get("/chapters/problems").text
    assert "No better copy was available" in page


def test_the_sweep_stands_aside_for_playback(check_env):
    _movie(PATH)
    set_setting("chapters_paused_since", "2026-01-01T00:00:00+00:00")
    assert filecheck_jobs.start() is False and filecheck_jobs.sweep() == 0


@pytest.mark.skipif(not tool("ffmpeg"), reason="needs FFmpeg")
def test_a_real_cut_off_download_is_found(tmp_path):
    whole = tmp_path / "whole.mkv"
    subprocess.run(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=10",
            "-t",
            "120",
            "-c:v",
            "libx264",
            "-g",
            "10",
            str(whole),
        ],
        check=True,
    )
    data = whole.read_bytes()
    cut = tmp_path / "cut.mkv"
    cut.write_bytes(data[: len(data) * 7 // 10])
    assert filecheck.grab(str(whole), 60) is not None
    assert filecheck.health(str(whole), 120, None, [], film=True) == []
    kinds = [i["kind"] for i in filecheck.health(str(cut), 120, None, [], film=True)]
    assert kinds == ["truncated"]


def test_nothing_is_left_unsaved_while_frames_are_read(app, check_env, monkeypatch):
    """Unsaved changes hold SQLite's write lock; the web app would wait on it."""
    seen = []

    def health(*args, **kwargs):
        seen.append(not database_free(app))
        return []

    monkeypatch.setattr(filecheck, "health", health)
    _movie(PATH)
    filecheck_jobs.sweep()
    assert seen == [False]


def test_a_short_file_named_as_one_part_says_so(monkeypatch):
    monkeypatch.setattr(filecheck, "grab", lambda path, t, width=640: frame())
    monkeypatch.setattr(filecheck, "_decode_errors", lambda path, t: False)
    name = "/films/Film.2009.Part.2.EXTENDED.1080p.BluRay.mkv"
    issue = filecheck.health(name, 93 * 60, 129, [], film=True)[0]
    assert "part 2 of a release split into parts" in issue["text"]
    other = filecheck.health("/films/Partisan.1080p.mkv", 93 * 60, 129, [], film=True)
    assert "different version" in other[0]["text"]


def test_replacing_one_part_of_a_split_film_ignores_split_releases(client, check_env):
    """The Millennium films' extended cuts come in two parts: blocklisting one
    part only made Radarr grab the other part next."""
    arr, found = check_env
    found["subtitles"] = "clean"
    found["issues"] = [
        {
            "kind": "short",
            "text": "Runs 93 min, but it's listed as 147 min: the file name says "
            "it's part 1 of a release split into parts, so the rest of the film "
            "is missing",
        }
    ]
    _library, media, _part = _movie(PATH)
    media.title = "The Girl Who Kicked the Hornet's Nest"
    db.session.commit()
    filecheck_jobs.sweep()
    state = db.session.query(ChapterState).one()
    page = client.post(
        f"/chapters/problems/{state.id}/replace", follow_redirects=True
    ).text
    assert "Looking for a better copy first" in page
    # The other part of the same release isn't a better copy.
    arr.available = [
        {"title": "Hornets.Nest.2009.Part.2.EXTENDED.1080p", "rejections": []},
        {"title": "Hornets.Nest.2009.EXTENDED.1080p.BluRay", "rejections": []},
    ]
    better_copy.sweep()
    page = client.get("/chapters/problems").text
    assert arr.calls[0] == (
        "ignore",
        7,
        "curatarr-split-7",
        "Curatarr: whole The Girl Who Kicked the Hornet's Nest, not one part",
    )


def test_a_film_with_a_part_in_its_title_is_never_ignored(client, check_env):
    arr, found = check_env
    found["subtitles"] = "clean"
    found["issues"] = [
        {
            "kind": "short",
            "text": "Runs 60 min: it's part 2 of a release split into parts",
        }
    ]
    _library, media, _part = _movie(PATH)
    media.title = "Harry Potter and the Deathly Hallows: Part 2"
    db.session.commit()
    filecheck_jobs.sweep()
    state = db.session.query(ChapterState).one()
    client.post(f"/chapters/problems/{state.id}/replace")
    better_copy.sweep()
    assert ("delete", 55) in arr.calls  # replaced, without ignoring "Part 2"
    assert not any(call[0] == "ignore" for call in arr.calls)


def test_split_release_pattern():
    import re

    pattern = re.compile(filecheck_jobs.SPLIT_RELEASE[1:-2], re.IGNORECASE)
    assert pattern.search("The.Girl.Who.Kicked.the.Hornets.Nest.2009.Part.1.EXTENDED")
    assert pattern.search("Film.2009.CD2.avi")
    assert pattern.search("The.Girl.Who.Kicked.the.Hornets.Nest.Part.II.2009.Extended")
    assert not pattern.search("Pirates.2006") and not pattern.search("Partition.2007")
    assert not pattern.search("Party.Down.2009") and not pattern.search(
        "Part.Time.2010"
    )


def test_the_row_shows_what_replace_is_doing(client, check_env):
    """Clicking Replace must visibly change the row: no second Replace while it's
    looked into, Cancel to take it back, and after "no better copy" just OK."""
    arr, _found = check_env
    _movie(PATH)
    filecheck_jobs.sweep()
    state = db.session.query(ChapterState).one()
    replace_url = f"/chapters/problems/{state.id}/replace"
    assert replace_url in client.get("/").text
    client.post(replace_url, data={"back": "overview"})
    page = client.get("/chapters/problems").text
    assert "Looking for a better copy…" in page and replace_url not in page
    assert f"/chapters/problems/{state.id}/cancel" in page
    # Being looked into doesn't need anyone, so Overview leaves it out.
    assert f"/chapters/problems/{state.id}" not in client.get("/").text
    # A second click (an old tab) changes nothing.
    page = client.post(replace_url, follow_redirects=True).text
    assert "Already looking for a better copy" in page
    assert len(setting("replace_queue")) == 1
    client.post(f"/chapters/problems/{state.id}/cancel")
    assert setting("replace_queue") == [] and replace_url in client.get("/").text
    # Nothing better: say so, offer only OK, never Replace again.
    arr.available = [{"title": "Film.2020.Swedish", "rejections": ["Not English"]}]
    client.post(replace_url)
    better_copy.sweep()
    page = client.get("/").text
    assert "No better copy was available" in page and replace_url not in page
    client.post(f"/chapters/problems/{state.id}/dismiss", data={"back": "overview"})
    assert replace_url not in client.get("/").text  # OK drops it
    assert filecheck_jobs.review()["to_act"] == []
