"""Chapter jobs (curatarr/enrichment.py) with fake media tools and integrations."""

import time
from datetime import timedelta

import pytest
from conftest import database_free

from curatarr import db, enrichment
from curatarr.chapters import Chapter
from curatarr.integrations import IntegrationError
from curatarr.mediafile import MediaInfo
from curatarr.models import (
    ChapterState,
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    utcnow,
)
from curatarr.services import set_setting

STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "h264"},
    {"index": 1, "codec_type": "audio", "codec_name": "eac3", "default": True},
]


class FakeFiles:
    """Media files by path: probe() reads them, write_chapters() edits them."""

    def __init__(self):
        self.files = {}
        self.writes = []
        self.removed = []

    def add(self, path, *, duration=3600.0, chapters=None, fmt="matroska,webm"):
        self.files[path] = {
            "duration": duration,
            "chapters": list(chapters or []),
            "format": fmt,
            "size": 1000,
            "mtime": time.time() - 3600,
        }

    def probe(self, path):
        f = self.files[path]
        return MediaInfo(
            path=path,
            size=f["size"],
            mtime=f["mtime"],
            duration=f["duration"],
            format_name=f["format"],
            chapters=[dict(c) for c in f["chapters"]],
            streams=[dict(s) for s in STREAMS],
        )

    def write(self, path, chapters):
        self.writes.append((path, chapters))
        f = self.files[path]
        f["chapters"] = [{"start": round(c.start, 3), "name": c.name} for c in chapters]
        f["size"] += 50
        f["mtime"] += 1  # changed by the edit, but long since imported

    def remove(self, path):
        self.removed.append(path)
        self.files[path]["chapters"] = []
        self.files[path]["mtime"] = time.time()


class FakeArr:
    def __init__(self):
        self.queue_records = []
        self.running = []
        self.sessions_list = []
        self.refreshed = []

    def queue(self):
        return self.queue_records

    def commands(self):
        return self.running

    def sessions(self):
        return self.sessions_list

    def refresh_item(self, item_id):
        self.refreshed.append(item_id)

    movie_record = None  # what Radarr says about the film; None: Radarr unreachable
    searched: tuple = ()

    def movie(self, movie_id):
        if self.movie_record is None:
            raise IntegrationError("Radarr unavailable")
        return self.movie_record

    def search_movie(self, movie_id):
        self.searched = (*self.searched, movie_id)


@pytest.fixture(autouse=True)
def not_paused():
    from curatarr import throttle

    throttle.set_paused(False)
    yield
    throttle.set_paused(False)


@pytest.fixture
def media_env(app, monkeypatch, tmp_path):
    files = FakeFiles()
    arr = FakeArr()
    monkeypatch.setattr(enrichment, "probe", files.probe)
    monkeypatch.setattr(enrichment, "write_chapters", files.write)
    monkeypatch.setattr(enrichment, "remove_chapters", files.remove)
    monkeypatch.setattr(enrichment, "client", lambda _kind: arr)
    monkeypatch.setattr(enrichment, "file_exists", lambda p: p in files.files)
    monkeypatch.setattr(
        enrichment,
        "file_identity",
        lambda p: (files.files[p]["size"], files.files[p]["mtime"]),
    )
    monkeypatch.setattr(
        enrichment,
        "can_write",
        lambda info: (info.matroska, None if info.matroska else "not MKV"),
    )
    monkeypatch.setattr(
        enrichment,
        "scene_changes",
        lambda info, threshold: (
            [(t * 60.0, 0.5) for t in (6.8, 14.2, 21.5, 28, 35, 42, 49, 55)],
            False,
        ),
    )
    return files, arr


@pytest.fixture
def identity_on(monkeypatch):
    """The episode check is off by default; these tests switch it on."""
    from curatarr import chapter_routes

    monkeypatch.setattr(enrichment, "identity_check_enabled", lambda: True)
    monkeypatch.setattr(chapter_routes, "identity_check_enabled", lambda: True)


def _movie(path="/movies/Film (2020)/Film.mkv", library_type="movies"):
    library = Library(
        jellyfin_library_id=f"lib-{library_type}",
        name="Movies",
        media_type=library_type,
    )
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title="Film",
        jellyfin_id="jf-film",
        radarr_id=7,
    )
    db.session.add(media)
    db.session.flush()
    part = MediaPart(
        media_identity_id=media.id,
        kind="movie_file",
        has_file=True,
        file_path=path,
        acquired_at=utcnow(),
    )
    db.session.add(part)
    db.session.commit()
    return library, media, part


def _navigation(library, **extra):
    set_setting(
        "chapter_library_settings",
        {library.id: {"enabled": True, "mode": "navigation", **extra}},
    )


def _run(part, action):
    enrichment.request(part, action)
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()  # jobs run in their own app context and session
    return db.session.query(ChapterState).filter_by(media_part_id=part.id).one()


def test_generates_scene_aligned_chapters_and_records_them(media_env):
    files, arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    state = _run(part, "generate")
    assert state.chapter_status == "generated_scene_aligned"
    assert state.chapter_source == "scene_aligned"
    assert state.chapter_count == len(files.files[part.file_path]["chapters"]) == 9
    assert state.chapter_generation_version == 1 and state.chapter_config_hash
    assert state.written_fingerprint and state.job_state == "idle"
    assert files.files[part.file_path]["chapters"][1]["start"] == 6.8 * 60
    assert arr.refreshed == ["jf-film"]  # only this item, not the library
    history = db.session.query(LifecycleAction).filter_by(
        action_type="chapters_generated"
    )
    assert history.count() == 1
    assert history.one().payload_json["method"] == "scene_aligned"


def test_existing_chapters_are_preserved(media_env):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path, chapters=[{"start": 0.0, "name": "Disc chapter"}])
    _navigation(library)
    state = _run(part, "generate")
    assert state.chapter_status == "existing" and state.chapter_source == "embedded"
    assert files.writes == []
    # Regenerating or removing someone else's chapters is refused.
    for action in ("regenerate", "remove"):
        state = _run(part, action)
        assert state.chapter_status == "failed" and "not written by Curatarr" in (
            state.chapter_error
        )
    assert files.writes == [] and files.removed == []


def test_short_files_and_other_containers_are_left_alone(media_env):
    files, _arr = media_env
    library, media, part = _movie()
    files.add(part.file_path, duration=15 * 60)
    _navigation(library)
    assert _run(part, "generate").chapter_status == "skipped_short"
    other = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=1,
        has_file=True,
        file_path="/movies/Film (2020)/Film.mp4",
    )
    db.session.add(other)
    db.session.commit()
    files.add(other.file_path, fmt="mov,mp4,m4a,3gp,3g2,mj2")
    state = _run(other, "generate")
    assert state.chapter_status == "unsupported_container"
    assert files.writes == []


def test_unchanged_file_is_not_processed_twice(media_env):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    _run(part, "generate")
    assert len(files.writes) == 1
    state = _run(part, "auto")
    assert state.chapter_status == "generated_scene_aligned"
    assert len(files.writes) == 1


def test_writes_wait_for_imports_playback_and_settling(media_env):
    files, arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    arr.queue_records = [{"movieId": 7, "status": "downloading"}]
    state = _run(part, "generate")
    assert state.job_state == "queued" and state.next_attempt_at
    assert state.chapter_error.startswith("Waiting:") and files.writes == []
    arr.queue_records = []
    arr.running = [
        {"name": "RenameMovie", "status": "started", "body": {"movieIds": [7]}}
    ]
    state.next_attempt_at = None
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    assert "RenameMovie" in state.chapter_error and files.writes == []
    arr.running = []
    # Pausing all chapter work during playback is tested separately; here the
    # per-file check (never edit the file being played) is what matters.
    set_setting("chapter_options", {"pause_during_playback": False})
    arr.sessions_list = [{"NowPlayingItem": {"Id": "jf-film"}}]
    state.next_attempt_at = None
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    assert "playing" in state.chapter_error and files.writes == []
    arr.sessions_list = []
    files.files[part.file_path]["mtime"] = time.time()
    state.next_attempt_at = None
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    assert "settle" in state.chapter_error and files.writes == []
    files.files[part.file_path]["mtime"] = time.time() - 3600
    state.next_attempt_at = None
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    assert state.chapter_status == "generated_scene_aligned"


def test_dry_run_and_preview_never_write(media_env):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library, processing={"dry_run": True})
    state = _run(part, "auto")
    assert state.chapter_status == "dry_run" and files.writes == []
    preview = state.last_preview_json
    assert preview["analysis"]["chosen"] == "scene_aligned"
    assert preview["write"]["command"][-2:] == [
        "--chapters",
        "<generated-chapters.txt>",
    ]
    assert preview["write"]["reencode"] is False
    state = _run(part, "preview")
    methods = [a["method"] for a in state.last_preview_json["analysis"]["attempts"]]
    assert "scene_aligned" in methods and "fixed" in methods
    assert files.writes == []


def test_remove_only_curatarr_chapters_and_restore_originals(media_env, monkeypatch):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    _run(part, "generate")
    # Someone edits the chapters afterwards: Curatarr no longer claims them.
    files.files[part.file_path]["chapters"][1]["name"] = "Edited"
    state = _run(part, "remove")
    assert state.chapter_status == "failed" and files.removed == []
    files.files[part.file_path]["chapters"][1]["name"] = "Chapter 2"
    state = _run(part, "remove")
    assert state.chapter_status == "removed" and files.removed == [part.file_path]
    assert state.written_fingerprint is None
    # A removed file is not regenerated automatically.
    assert enrichment.queue_new_media() == 0


def test_replacing_foreign_chapters_keeps_a_copy(media_env, monkeypatch):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path, chapters=[{"start": 0.0, "name": "Disc"}])
    _navigation(library, preserve_existing=False)
    monkeypatch.setattr(enrichment, "export_chapters_xml", lambda path: "<Chapters/>")
    restored = []
    monkeypatch.setattr(
        enrichment, "restore_chapters_xml", lambda path, xml: restored.append(xml)
    )
    state = _run(part, "generate")
    assert state.chapter_status == "generated_scene_aligned"
    assert state.original_chapters_xml == "<Chapters/>"
    state = _run(part, "remove")
    assert restored == ["<Chapters/>"] and state.chapter_status == "existing"


def test_new_media_is_queued_only_after_enabling(media_env):
    files, _arr = media_env
    library, _media, old = _movie()
    old.acquired_at = utcnow() - timedelta(days=30)
    db.session.commit()
    files.add(old.file_path)
    enrichment.save_settings(
        library,
        {"chapter_mode": "navigation"},
    )
    assert enrichment.queue_new_media() == 0  # existing library is not swept
    new = MediaPart(
        media_identity_id=old.media_identity_id,
        kind="episode",
        season_number=1,
        episode_number=2,
        has_file=True,
        file_path="/movies/new.mkv",
        acquired_at=utcnow(),
    )
    db.session.add(new)
    db.session.commit()
    files.add(new.file_path)
    assert enrichment.queue_new_media() == 1
    assert enrichment.queue_new_media() == 0  # already queued
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=new.id).one()
    assert state.chapter_status == "generated_scene_aligned"
    assert enrichment.queue_new_media() == 0
    # An upgrade (newer import of the same episode) is processed again.
    new.acquired_at = utcnow() + timedelta(minutes=5)
    db.session.commit()
    assert enrichment.queue_new_media() == 1


def test_files_a_scan_settled_still_get_subtitles(media_env):
    """A scan marks files with chapters of their own as handled, so no
    chapter job runs, and subtitles used to follow only chapter jobs."""
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path, chapters=[{"start": 0.0, "name": "Disc"}])
    enrichment.save_settings(
        library,
        {
            "chapter_mode": "navigation",
            "existing_library": "true",
            "subtitles_enabled": "true",
        },
    )
    state = _run(part, "scan")
    assert state.chapter_status == "existing" and state.subtitle_status is None
    assert enrichment.queue_new_media() == 1
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert (state.job_state, state.requested_action) == ("queued", "subtitles")
    assert enrichment.queue_new_media() == 0  # already queued
    # Once checked, nothing more is queued for it.
    state.job_state, state.requested_action = "idle", None
    state.subtitle_status = "existing"
    db.session.commit()
    assert enrichment.queue_new_media() == 0


def test_failed_semantic_step_falls_back_to_scene_alignment(media_env, monkeypatch):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    set_setting(
        "chapter_library_settings",
        {
            library.id: {
                "enabled": True,
                "mode": "smart",
                "semantic": {"llm_model": "qwen3:14b"},
            }
        },
    )

    def no_transcriber(*_args, **_kwargs):
        from curatarr.transcribe import TranscriptionUnavailable

        raise TranscriptionUnavailable("faster-whisper is not installed")

    monkeypatch.setattr("curatarr.transcribe.transcribe", no_transcriber)
    state = _run(part, "generate")
    assert state.chapter_status == "generated_scene_aligned"
    attempts = state.last_preview_json["analysis"]["attempts"]
    assert attempts[0] == {
        "method": "semantic",
        "ok": False,
        "error": "faster-whisper is not installed",
    }


def test_smart_chapters_with_a_fake_model(media_env, monkeypatch):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    set_setting(
        "chapter_library_settings",
        {library.id: {"enabled": True, "mode": "smart"}},
    )
    from curatarr.chapters import Segment

    segments = [Segment(t, t + 15, f"line {t}") for t in range(0, 3600, 20)]
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe",
        lambda *_a, **_k: (segments, {"cached": False, "device": "cpu"}),
    )

    class FakeModel:
        model = "fake"
        calls = 0

        def ask(self, prompt, schema):
            self.calls += 1
            if "choice" in schema["properties"]:
                return {"choice": "A"}
            return {"summary": "Something happens.", "name": f"Scene {self.calls}"}

    enrichment.request(part, "generate")
    enrichment.dispatch(synchronous=True, llm=FakeModel())
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert state.chapter_status == "generated_semantic"
    names = [c["name"] for c in files.files[part.file_path]["chapters"]]
    assert all(name.startswith("Scene ") for name in names)


def test_local_path_mapping_prefers_the_longest_prefix():
    mappings = [
        {"arr": "/data", "local": "/mnt/nas"},
        {"arr": "/data/tv", "local": "/mnt/tv"},
    ]
    assert (
        enrichment.local_path("/data/tv/Show/x.mkv", mappings) == "/mnt/tv/Show/x.mkv"
    )
    assert (
        enrichment.local_path("/data/movies/x.mkv", mappings) == "/mnt/nas/movies/x.mkv"
    )
    assert enrichment.local_path("/database/x.mkv", mappings) == "/database/x.mkv"
    assert (
        enrichment.local_path("D:\\Media\\x.mkv", [{"arr": "D:\\Media", "local": "/m"}])
        == "/m/x.mkv"
    )


def test_maintenance_scan_counts_without_generating(media_env):
    files, _arr = media_env
    _library, media, part = _movie()
    files.add(part.file_path, chapters=[{"start": 0.0, "name": "A"}])
    second = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=1,
        has_file=True,
        file_path="/movies/b.mkv",
    )
    third = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=2,
        has_file=True,
        file_path="/movies/c.mkv",
    )
    db.session.add_all([second, third])
    db.session.commit()
    files.add(second.file_path)
    files.add(third.file_path, duration=600)
    assert enrichment.queue_scan() == 3
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    summary = enrichment.maintenance_summary()
    assert summary["scanned"] == 3 and summary["existing"] == 1
    assert summary["needing"] == 1 and summary["short"] == 1
    assert files.writes == []
    needing = enrichment.needs_chapters_query().one()
    assert needing.media_part_id == second.id


def test_maintenance_summary_counts_files_needing_subtitles(media_env):
    files, _arr = media_env
    _library, media, part = _movie()
    files.add(part.file_path)
    never_checked = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=1,
        has_file=True,
        file_path="/movies/b.mkv",
    )
    already_has = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=2,
        has_file=True,
        file_path="/movies/c.mkv",
    )
    db.session.add_all([never_checked, already_has])
    db.session.commit()
    files.add(never_checked.file_path)
    files.add(already_has.file_path)
    state = enrichment.state_for(already_has)
    state.subtitle_status = "existing"
    db.session.commit()

    summary = enrichment.maintenance_summary()
    assert summary["needing_subtitles"] == 2  # part (never scanned) + never_checked
    assert summary["subtitles_possible"] == 3

    enrichment.queue_subtitles()
    db.session.commit()
    summary = enrichment.maintenance_summary()
    assert summary["needing_subtitles"] == 0  # both now queued, not idle
    assert summary["subtitles_queued"] == 2
    # Queued files are still to do: the bar mustn't read 100% while they wait.
    assert summary["subtitles_have"] == 1
    assert summary["subtitles_possible"] == 3


def test_pages_render_and_settings_save(app, client, media_env):
    files, _arr = media_env
    library, media, part = _movie()
    files.add(part.file_path)
    assert client.get("/chapters/settings").status_code == 200
    response = client.post(
        "/chapters/settings",
        data={
            "chapter_mode": "smart",
            "minimum_duration_minutes": "20",
            "target_spacing_minutes": "7",
            "minimum_spacing_minutes": "4",
            "maximum_spacing_minutes": "10",
            "preserve_existing": "true",
            "scene_detection": "true",
            "fixed_intervals": "true",
            "semantic_enabled": "true",
            "whisper_model": "medium",
            "llm_model": "qwen3:14b",
            "semantic_names": "true",
            "fallback_format": "Chapter {number}",
            "new_media": "true",
            "concurrency": "1",
            "settle_minutes": "10",
            "scene_threshold": "0.3",
            "ollama_url": "http://127.0.0.1:11434",
            "whisper_device": "auto",
            "path_mappings": "/data => /mnt/data",
        },
    )
    assert response.status_code == 302
    config = enrichment.config_for(library.id)
    assert config["enabled"] and config["mode"] == "smart"
    assert config["processing"]["existing_library"] is False  # unchecked box
    assert enrichment.options()["path_mappings"] == [
        {"arr": "/data", "local": "/mnt/data"}
    ]
    # A library override to Navigation, everything else inherited.
    client.post(
        f"/chapters/settings?scope={library.id}",
        data={"chapter_mode": "navigation", "target_spacing_minutes": ""},
    )
    assert enrichment.config_for(library.id)["mode"] == "navigation"
    assert enrichment.config_for(library.id)["semantic"]["llm_model"] == "qwen3:14b"
    bad = client.post(
        f"/chapters/settings?scope={library.id}",
        data={"chapter_mode": "navigation", "minimum_spacing_minutes": "9"},
        follow_redirects=True,
    )
    assert b"minimum" in bad.data.lower()
    for url in (
        "/chapters",
        f"/chapters?library={library.id}",
        f"/chapters/parts/{part.id}",
        f"/titles/{media.id}",
        f"/chapters/libraries/{library.id}/process",
    ):
        assert client.get(url).status_code == 200, url
    page = client.get(f"/titles/{media.id}").get_data(as_text=True)
    assert "Chapters" in page and "Preview and actions" in page


def test_removal_is_refused_from_the_page_without_proof(app, client, media_env):
    files, _arr = media_env
    _library, _media, part = _movie()
    files.add(part.file_path)
    response = client.post(f"/chapters/parts/{part.id}/remove", follow_redirects=True)
    assert b"only regenerate or remove chapters it wrote" in response.data
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert state.job_state == "idle"


def test_part_page_shows_a_preview(app, client, media_env):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    _run(part, "preview")
    page = client.get(f"/chapters/parts/{part.id}").get_data(as_text=True)
    assert "Write operation (not performed)" in page
    assert "CHAPTER01=00:00:00.000" in page
    assert "Scene-aligned" in page and "Fixed interval" in page


def test_chapter_dataclass_round_trip():
    assert Chapter(1.23456, "x").as_dict() == {"start": 1.235, "name": "x"}


def test_scan_before_discovery_requests_it_then_scans(
    app, client, media_env, monkeypatch
):
    from curatarr.services import setting

    files, _arr = media_env
    _library, _media, part = _movie()
    part.file_path = None  # recorded by the first discovery after upgrading
    db.session.commit()
    page = client.post("/chapters/scan", follow_redirects=True).get_data(as_text=True)
    assert "has not recorded where your files are" in page
    assert setting("reconcile_requested_at")
    part.file_path = "/movies/Film (2020)/Film.mkv"
    db.session.commit()
    files.add(part.file_path)
    assert enrichment.run_requested_scan() == 1
    assert enrichment.run_requested_scan() == 0  # only once


def test_a_busy_database_requeues_the_job_instead_of_failing(media_env, monkeypatch):
    from sqlalchemy.exc import OperationalError

    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)

    def locked(*_args, **_kwargs):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(enrichment, "run_job", locked)
    enrichment.request(part, "generate")
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert state.job_state == "queued" and state.chapter_status == "not_processed"


def test_maintenance_page_shows_activity(app, client, media_env):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    page = client.get("/chapters").get_data(as_text=True)
    assert "Idle: nothing is queued." in page
    state = enrichment.request(part, "generate")
    state.job_state = "running"
    db.session.commit()
    page = client.get("/chapters").get_data(as_text=True)
    assert "Working" in page and "Generate" in page and "Film" in page
    state.job_state = "queued"
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    page = client.get("/chapters").get_data(as_text=True)
    assert "Last 1 finished" in page and "generated scene aligned" in page


def test_maintenance_page_shows_recent_subtitle_checks_instead_of_idle(
    app, client, media_env
):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    state = enrichment.request(part, "generate")
    state.subtitle_status = "existing"
    state.subtitle_processed_at = utcnow()
    db.session.commit()
    page = client.get("/chapters").get_data(as_text=True)
    assert "Nothing running right now" not in page
    assert "Working" in page and "Subtitles" in page and "Film" in page

    state.subtitle_processed_at = utcnow() - timedelta(seconds=30)
    db.session.commit()
    page = client.get("/chapters").get_data(as_text=True)
    assert "Nothing running right now" in page


def test_chapter_activity_fragment_matches_the_page(app, client, media_env):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    fragment = client.get("/chapters/activity").get_data(as_text=True)
    assert 'id="chapter-activity"' in fragment
    assert "Idle: nothing is queued." in fragment


def test_settings_warn_when_smart_chapters_has_no_model(app, client):
    set_setting("chapter_settings", {"enabled": True, "mode": "smart"})
    assert "No Ollama model is set" in client.get("/chapters/settings").get_data(
        as_text=True
    )
    set_setting(
        "chapter_settings",
        {"enabled": True, "mode": "smart", "semantic": {"llm_model": "qwen3:14b"}},
    )
    assert "No Ollama model is set" not in client.get("/chapters/settings").get_data(
        as_text=True
    )


def test_chapter_work_makes_room_for_someone_watching(
    app, client, media_env, monkeypatch
):
    from curatarr import adaptive, throttle

    files, arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    enrichment.request(part, "generate")
    # Someone is watching and the machine is already busy: nothing new starts.
    arr.sessions_list = [{"NowPlayingItem": {"Id": "something-else"}}]
    monkeypatch.setattr(adaptive, "cpu_use", lambda: 0.9)
    assert enrichment.dispatch(synchronous=True) == 0 and files.writes == []
    assert "No new chapter jobs" in client.get("/chapters").get_data(as_text=True)
    # Still watching, but the machine has room: work carries on.
    monkeypatch.setattr(adaptive, "cpu_use", lambda: 0.1)
    assert enrichment.dispatch(synchronous=True) == 1
    assert not throttle.paused() and len(files.writes) == 1


def test_running_jobs_pause_when_playback_needs_the_room(
    app, client, media_env, monkeypatch
):
    from curatarr import adaptive, throttle

    files, arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    from curatarr import leases

    state = enrichment.request(part, "generate")
    state.job_state = "running"
    db.session.commit()
    assert leases.acquire(f"chapters:{state.id}", 600)  # a live job, not a stale one
    arr.sessions_list = [
        {"NowPlayingItem": {"Id": "x"}, "PlayState": {"PlayMethod": "Transcode"}}
    ]
    monkeypatch.setattr(adaptive, "cpu_use", lambda: 0.98)
    monkeypatch.setattr(adaptive.time, "monotonic", lambda: 1000.0)
    enrichment.dispatch(synchronous=True)  # the overload starts
    monkeypatch.setattr(adaptive.time, "monotonic", lambda: 1040.0)
    assert enrichment.dispatch(synchronous=True) == 0  # and has lasted 40 s
    assert throttle.paused()
    page = client.get("/chapters").get_data(as_text=True)
    assert "to keep playback smooth: CPU 98%" in page and "1 transcoding" in page
    arr.sessions_list = []
    monkeypatch.setattr(adaptive, "cpu_use", lambda: 0.1)
    enrichment.dispatch(synchronous=True)
    assert not throttle.paused()


def test_paused_processes_are_suspended_and_their_time_not_counted():
    import subprocess
    import threading
    import time

    from curatarr import throttle

    throttle.set_paused(True)
    threading.Timer(1.5, throttle.set_paused, args=(False,)).start()
    started = time.monotonic()
    # 1 s of work with a 1.2 s limit: it only finishes because the suspended
    # time before it could start running is not counted.
    result = throttle.run(["/bin/sh", "-c", "sleep 1; echo done"], timeout=1.2)
    assert result.stdout.strip() == "done" and time.monotonic() - started >= 2.4
    with pytest.raises(subprocess.TimeoutExpired):
        throttle.run(["/bin/sleep", "10"], timeout=0.5)


def test_saving_a_page_without_the_pause_option_keeps_it(app, client):
    from curatarr.enrichment import options

    form = {
        "chapter_mode": "smart",
        "minimum_duration_minutes": "20",
        "target_spacing_minutes": "7",
        "minimum_spacing_minutes": "4",
        "maximum_spacing_minutes": "10",
        "whisper_model": "medium",
        "fallback_format": "Chapter {number}",
        "fixed_intervals": "true",
        "concurrency": "2",
    }
    client.post("/chapters/settings", data=form)  # a page from before the option
    assert options()["concurrency"] == 2  # the save went through
    assert options()["pause_during_playback"] is True
    client.post(
        "/chapters/settings", data=form | {"pause_during_playback_shown": "1"}
    )  # shown and unticked
    assert options()["pause_during_playback"] is False
    client.post(
        "/chapters/settings",
        data=form
        | {"pause_during_playback_shown": "1", "pause_during_playback": "true"},
    )
    assert options()["pause_during_playback"] is True


def test_process_all_libraries_queues_only_enabled_ones(app, client, media_env):
    files, _arr = media_env
    library, _media, part = _movie()
    other = Library(jellyfin_library_id="lib-other", name="Other", media_type="tv")
    db.session.add(other)
    db.session.flush()
    show = MediaIdentity(library_id=other.id, media_type="series", title="Show")
    db.session.add(show)
    db.session.flush()
    episode = MediaPart(
        media_identity_id=show.id,
        kind="episode",
        season_number=1,
        episode_number=1,
        has_file=True,
        file_path="/tv/show.mkv",
    )
    db.session.add(episode)
    db.session.commit()
    files.add(part.file_path)
    files.add(episode.file_path)
    _navigation(library)  # only the movie library has chapters enabled
    enrichment.queue_scan()
    enrichment.dispatch(synchronous=True)
    page = client.get("/chapters").get_data(as_text=True)
    assert "Process all libraries" in page
    confirm = client.get("/chapters/libraries/all/process").get_data(as_text=True)
    assert (
        "every library with chapters enabled" in confirm and "Queue 1 file<" in confirm
    )
    assert "Adds 1 file without chapters" in client.get("/chapters/files").text
    client.post("/chapters/libraries/all/process", data={"confirm": "1"})
    db.session.expire_all()
    queued = db.session.query(ChapterState).filter_by(job_state="queued").all()
    assert [state.media_part_id for state in queued] == [part.id]
    # Everything is queued now: nothing to add, so no button, just a note.
    files_page = client.get("/chapters/files").text
    assert (
        "Process all…" not in files_page
        and "The 1 file needing chapters is already queued" in files_page
    )
    confirm = client.get("/chapters/libraries/all/process").text
    assert "Queue 0 files" in confirm and "1 more is already queued" in confirm


class _UnavailableModel:
    model = "fake"
    calls = 0

    def ask(self, prompt, schema):
        from curatarr.semantic import LanguageModelUnavailable

        raise LanguageModelUnavailable("Ollama request failed")


def _smart_library_with_transcript(library, monkeypatch):
    from curatarr.chapters import Segment

    set_setting(
        "chapter_library_settings",
        {library.id: {"enabled": True, "mode": "smart"}},
    )
    segments = [Segment(t, t + 15, f"line {t}") for t in range(0, 3600, 20)]
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe",
        lambda *_a, **_k: (segments, {"cached": True, "device": "cuda"}),
    )


def _dispatch_again(part, llm):
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    state.next_attempt_at = None  # due now instead of in 15 minutes
    db.session.commit()
    enrichment.dispatch(synchronous=True, llm=llm)
    db.session.expire_all()
    return db.session.query(ChapterState).filter_by(media_part_id=part.id).one()


def test_ollama_unavailable_waits_before_settling_for_navigation(
    media_env, monkeypatch
):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _smart_library_with_transcript(library, monkeypatch)
    enrichment.request(part, "generate")
    enrichment.dispatch(synchronous=True, llm=_UnavailableModel())
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert state.job_state == "queued" and state.attempts == 1
    assert state.chapter_error.startswith("Waiting: Ollama did not answer")
    assert not files.files[part.file_path]["chapters"]

    assert _dispatch_again(part, _UnavailableModel()).attempts == 2
    # The last attempt writes Navigation chapters rather than nothing.
    state = _dispatch_again(part, _UnavailableModel())
    assert state.chapter_status == "generated_scene_aligned"
    assert state.attempts == 0


def test_navigation_fallbacks_can_be_redone_with_smart_chapters(media_env, monkeypatch):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    assert _run(part, "generate").chapter_status == "generated_scene_aligned"
    assert enrichment.maintenance_summary()["smart_upgradable"] == 0  # not Smart

    _smart_library_with_transcript(library, monkeypatch)
    assert enrichment.maintenance_summary()["smart_upgradable"] == 1
    assert enrichment.queue_smart_upgrade() == 1

    class Model:
        model = "fake"
        calls = 0

        def ask(self, prompt, schema):
            self.calls += 1
            if "choice" in schema["properties"]:
                return {"choice": "A"}
            return {"summary": "Something happens.", "name": f"Scene {self.calls}"}

    enrichment.dispatch(synchronous=True, llm=Model())
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert state.chapter_status == "generated_semantic"
    assert enrichment.maintenance_summary()["smart_upgradable"] == 0


def test_quick_jobs_run_before_the_backlog(media_env):
    _library, media, _part = _movie()
    rows = []
    for action in ("generate", "auto", "regenerate", "subtitles"):
        other = MediaPart(
            media_identity_id=media.id,
            kind="movie",
            has_file=True,
            file_path=f"/movies/{action}.mkv",
        )
        db.session.add(other)
        db.session.flush()
        state = enrichment.state_for(other)
        state.job_state, state.requested_action = "queued", action
        state.requested_at = utcnow()  # later requests, higher priority
        rows.append(state)
    db.session.commit()
    due = enrichment._due_jobs(4, scans=False)
    assert [row.requested_action for row in due] == [
        "subtitles",
        "regenerate",
        "auto",
        "generate",
    ]


def test_redo_takes_over_a_file_waiting_only_for_subtitles(media_env, monkeypatch):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    state = _run(part, "generate")
    state.job_state, state.requested_action = "queued", "subtitles"
    db.session.commit()
    _smart_library_with_transcript(library, monkeypatch)
    assert enrichment.maintenance_summary()["smart_upgradable"] == 1
    assert enrichment.queue_smart_upgrade() == 1
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert state.requested_action == "regenerate"


def test_missing_faster_whisper_waits_instead_of_writing_navigation_chapters(
    app, client, media_env, monkeypatch
):
    files, _arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    set_setting(
        "chapter_library_settings", {library.id: {"enabled": True, "mode": "smart"}}
    )
    monkeypatch.setattr("curatarr.transcribe.available", lambda: False)
    monkeypatch.setattr("curatarr.transcribe.cached", lambda *_a, **_k: None)
    state = _run(part, "generate")
    assert state.job_state == "queued" and state.attempts == 0
    assert "without faster-whisper" in state.chapter_error
    assert not files.files[part.file_path]["chapters"]
    assert enrichment.smart_chapters_blocked()
    assert "Smart Chapters is stopped" in client.get("/chapters").get_data(as_text=True)

    _navigation(library)  # Navigation libraries never needed transcription
    state.next_attempt_at = None
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    assert (
        db.session.get(ChapterState, state.id).chapter_status
        == "generated_scene_aligned"
    )
    assert not enrichment.smart_chapters_blocked()


def test_forecast_projects_the_finish_from_the_recent_pace(app, client, media_env):
    _files, _arr = media_env
    library, media, _part = _movie()
    _navigation(library)
    now = utcnow()
    assert enrichment.job_forecast(now) is None
    states = []
    for n in range(10):
        part = MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            file_path=f"/movies/Film (2020)/Film {n}.mkv",
            acquired_at=now,
        )
        db.session.add(part)
        db.session.flush()
        states.append(enrichment.state_for(part))
    for n, state in enumerate(states[:6]):  # six finished in a day-old batch
        state.requested_at = now - timedelta(hours=30)
        state.chapter_processed_at = now - timedelta(minutes=30 * n)
    for state in states[6:]:  # four still to do, one a fast subtitle check
        state.job_state, state.requested_action = "queued", "generate"
        state.requested_at = now - timedelta(hours=30)
    states[-1].requested_action = "subtitles"
    db.session.commit()
    forecast = enrichment.job_forecast(now)
    assert forecast["remaining"] == 3 and forecast["done"] == 6
    assert forecast["per_hour"] == 0.25 and forecast["left"] == "12 h"
    assert forecast["percent"] == round(100 * 6 / 9, 1)
    page = client.get("/chapters").get_data(as_text=True)
    assert "6</strong> of 9 chapter jobs done" in page and "12 h" in page
    # A native <progress>: the page's CSP blocks inline style="width: …%".
    assert '<progress class="w-100 d-block" value="6" max="9"' in page
    assert 'style="width' not in page


def test_forecast_goes_by_hours_of_video(media_env):
    _library, media, _part = _movie()
    now = utcnow()
    for n in range(9):
        part = MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            file_path=f"/movies/Film (2020)/Film {n}.mkv",
            acquired_at=now,
        )
        db.session.add(part)
        db.session.flush()
        state = enrichment.state_for(part)
        state.requested_at = now - timedelta(hours=30)
        if n < 6:  # six half-hour episodes finished over the day
            state.duration_seconds = 1800
            state.chapter_processed_at = now - timedelta(minutes=30 * n)
        else:  # three two-hour films to go, one not yet measured
            state.job_state, state.requested_action = "queued", "generate"
            state.duration_seconds = 7200 if n < 8 else None
    db.session.commit()
    forecast = enrichment.job_forecast(now)
    # 3 hours of video a day; 6 hours left = 2 days (by files it would say 12 h).
    assert forecast["video_per_hour"] == 0.125 and forecast["left"] == "2 days"


def test_forecast_waits_for_enough_finished_jobs(media_env):
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.job_state, state.requested_action, state.requested_at = (
        "queued",
        "generate",
        utcnow(),
    )
    db.session.commit()
    forecast = enrichment.job_forecast()
    assert forecast["remaining"] == 1 and forecast["left"] is None


def test_duration_words():
    assert (
        enrichment._duration_words(timedelta(days=5, hours=13, minutes=20))
        == "5 days 13 h"
    )
    assert enrichment._duration_words(timedelta(minutes=50)) == "50 min"
    assert enrichment._duration_words(timedelta(hours=4, minutes=10)) == "4 h"


def test_maintenance_page_separates_to_do_from_library_stats(app, client, media_env):
    page = client.get("/chapters").get_data(as_text=True)
    done = page.split('id="done-heading"')[1].split('id="todo-heading"')[0]
    todo, library = page.split('id="todo-heading"')[1].split('id="library-heading"')
    assert "Chapters added" in done and "Bad skip markers hidden" in done
    assert "Needing chapters" in todo and "Chapter jobs queued or running" in todo
    assert "Files" not in todo
    assert "Too short for chapters" in library and "Needing chapters" not in library
    assert "stat-done" in todo  # nothing to do yet shows as done


def test_maintenance_page_shows_how_much_curatarr_has_covered(app, client, media_env):
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.duration_seconds, state.chapter_status = 5400, "generated_semantic"
    state.subtitle_status = "existing"
    _library2, _media2, part2 = _movie("/movies/Other (2021)/Other.mkv", "homevideos")
    other = enrichment.state_for(part2)
    other.duration_seconds, other.chapter_status = 5400, "existing"
    db.session.commit()
    summary = enrichment.maintenance_summary()
    assert (summary["chapters_have"], summary["chapters_possible"]) == (2, 2)
    page = client.get("/chapters").get_data(as_text=True)
    done = page.split('id="done-heading"')[1].split('id="todo-heading"')[0]
    assert "Files with chapters" in done and "<strong>2</strong> of 2 files" in done
    assert "1 already had them" in done and "1 added by Curatarr" in done
    assert "all done" in done
    assert 'style="' not in done  # the page's CSP blocks inline styles


def test_waiting_subtitle_jobs_show_their_reason(app, client, media_env):
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.job_state, state.requested_action = "queued", "subtitles"
    state.next_attempt_at = utcnow() + timedelta(minutes=5)
    state.subtitle_error = "Waiting: Sonarr is still importing this file"
    db.session.commit()
    page = client.get("/chapters").get_data(as_text=True)
    assert "Film — Sonarr is still importing this file" in page


def _series_with_recaps(files):
    """A Smart TV series: three episodes already transcribed, one to check."""
    import json

    from curatarr.chapters import fingerprint

    library = Library(jellyfin_library_id="lib-tv", name="TV", media_type="tvshows")
    db.session.add(library)
    db.session.flush()
    show = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title="Island Life",
        jellyfin_id="jf-show",
    )
    db.session.add(show)
    db.session.flush()
    set_setting(
        "chapter_library_settings", {library.id: {"enabled": True, "mode": "smart"}}
    )
    transcripts = enrichment.cache_dir() / "transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    parts = []
    for number, left in enumerate((18, 17, 16, 15), start=1):
        part = MediaPart(
            media_identity_id=show.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
            file_path=f"/tv/Island Life/S01E0{number}.mkv",
            jellyfin_id=f"jf-ep{number}",
            acquired_at=utcnow() - timedelta(days=30),
        )
        db.session.add(part)
        db.session.flush()
        state = enrichment.state_for(part)
        rows = [
            {"start": 0.5, "end": 3.0, "text": "Previously on Island Life."},
            {
                "start": 3.0,
                "end": 30.0,
                "text": f"On day {40 - left} storm number {left} hit.",
            },
            {
                "start": 77.5,
                "end": 82.0,
                "text": f"{left} are left, who will be voted out tonight?",
            },
            {
                "start": 90.0,
                "end": 99.0,
                "text": f"Episode {number} begins with breakfast {left}.",
            },
        ]
        if number < 4:
            key = f"{number:032d}"
            (transcripts / f"{key}.json").write_text(
                json.dumps({"segments": rows, "details": {}})
            )
            state.transcript_key = key
        else:
            target_rows = rows
        chapters = [Chapter(0.0, "Breakfast Plans"), Chapter(900.0, "The Challenge")]
        files.add(part.file_path, chapters=[c.as_dict() for c in chapters])
        state.written_fingerprint = fingerprint([c.as_dict() for c in chapters])
        state.chapter_status = "generated_semantic"
        parts.append(part)
    db.session.commit()
    return parts, target_rows


def test_recap_job_adds_previously_on_and_marks_intro_skipper(media_env, monkeypatch):
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, rows = _series_with_recaps(files)
    target = parts[-1]
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe",
        lambda *_a, **_k: (
            [Segment(r["start"], r["end"], r["text"]) for r in rows],
            {},
        ),
    )
    segments = {"jf-ep4": [{"Id": "guess", "Type": "Recap", "Start": 0, "End": 40}]}
    calls = []

    def plugin(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        item = path.split("/")[2]
        if method == "GET":
            return segments.get(item, [])
        if method == "POST":
            return {"Id": "ours", **kwargs["json"]}
        return None

    arr.request = plugin
    enrichment.request(target, "recap")
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=target.id).one()
    assert (state.recap_start, state.recap_end, state.recap_source) == (
        0.0,
        82.0,
        "closing line",
    )
    assert state.recap_segment_id == "ours" and state.job_state == "idle"
    assert ("DELETE", "/Episode/jf-ep4/Segments/guess", None) in calls
    assert (
        "POST",
        "/Episode/jf-ep4/Segments",
        {"Type": "Recap", "Start": 0.0, "End": 82.0},
    ) in calls
    chapters = files.files[target.file_path]["chapters"]
    assert chapters[:2] == [
        {"start": 0.0, "name": "Previously On"},
        {"start": 82.0, "name": "Breakfast Plans"},
    ]
    assert (
        state.chapter_status == "generated_semantic"
    )  # a recap check isn't a chapter job
    assert enrichment.curatarr_wrote(state, files.probe(target.file_path))


def test_episodes_without_recaps_get_nothing_and_series_are_skipped(
    media_env, monkeypatch
):
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, _rows = _series_with_recaps(files)
    plain = [
        Segment(0, 5, "Okay team, first day on the job."),
        Segment(5, 9, "Coffee?"),
    ]
    monkeypatch.setattr("curatarr.transcribe.transcribe", lambda *_a, **_k: (plain, {}))
    arr.request = lambda method, path, **kwargs: []
    enrichment.request(parts[-1], "recap")
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=parts[-1].id).one()
    assert state.recap_end is None and state.recap_source == "no opener"
    assert files.files[parts[-1].file_path]["chapters"][0]["name"] == "Breakfast Plans"
    assert enrichment.recaps_to_check() == 3
    assert enrichment.queue_recaps() == 3


def test_smart_generation_finds_the_recap_in_the_same_pass(media_env, monkeypatch):
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, rows = _series_with_recaps(files)
    target = parts[-1]
    files.files[target.file_path]["chapters"] = []
    segments = [Segment(r["start"], r["end"], r["text"]) for r in rows] + [
        Segment(t, t + 15, f"line {t}") for t in range(120, 3600, 20)
    ]
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe",
        lambda *_a, **_k: (segments, {"cached": False, "device": "cpu"}),
    )
    arr.request = lambda method, path, **kwargs: (
        {"Id": "ours"} if method == "POST" else []
    )

    class FakeModel:
        model = "fake"
        calls = 0

        def ask(self, prompt, schema):
            self.calls += 1
            if "choice" in schema["properties"]:
                return {"choice": "A"}
            return {"summary": "Something happens.", "name": f"Scene {self.calls}"}

    enrichment.request(target, "generate")
    enrichment.dispatch(synchronous=True, llm=FakeModel())
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=target.id).one()
    chapters = files.files[target.file_path]["chapters"]
    assert chapters[0] == {"start": 0.0, "name": "Previously On"}
    assert chapters[1]["start"] == 82.0 and chapters[1]["name"].startswith("Scene ")
    assert state.recap_end == 82.0 and state.recap_segment_id == "ours"
    assert state.transcript_key  # so later episodes can learn from this one


def test_recap_checks_run_as_one_batch_apart_from_chapter_jobs(app, client, media_env):
    _files, _arr = media_env
    _library, media, _part = _movie()
    for n in range(6):
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=1,
            episode_number=n,
            has_file=True,
            file_path=f"/tv/e{n}.mkv",
        )
        db.session.add(part)
        db.session.flush()
        state = enrichment.state_for(part)
        state.job_state = "queued"
        state.requested_action = "recap" if n < 4 else "generate"
        state.transcript_key = "b" * 32 if n < 4 else None  # no GPU needed to check
        state.requested_at = utcnow() - timedelta(minutes=10 - n)
    db.session.commit()
    assert [s.requested_action for s in enrichment._due_jobs(10, scans=False)] == [
        "generate",
        "generate",
    ]
    batch = enrichment._due_jobs(100, scans=False, recaps=True)
    assert len(batch) == 4 and {s.requested_action for s in batch} == {"recap"}
    batch[0].markers_checked_at = utcnow()
    db.session.commit()
    page = client.get("/chapters").get_data(as_text=True)
    assert (
        "Check skip markers</strong>" in page
        and "Nothing running right now" not in page
    )


def test_worker_start_requeues_jobs_the_last_worker_left_running(media_env):
    from curatarr import leases
    from curatarr.models import JobLease

    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.job_state, state.requested_action = "running", "generate"
    db.session.commit()
    assert leases.acquire(f"chapters:{state.id}", 600)
    assert enrichment.release_abandoned_jobs() == 1
    db.session.expire_all()
    assert db.session.get(ChapterState, state.id).job_state == "queued"
    assert (
        db.session.query(JobLease).filter_by(scope=f"chapters:{state.id}").count() == 0
    )


def test_find_recaps_button_hides_while_everything_is_queued(app, client, media_env):
    files, _arr = media_env
    _series_with_recaps(files)
    page = client.get("/chapters").get_data(as_text=True)
    assert "Check skip markers on 4 more</button>" in page
    assert enrichment.queue_recaps() == 4
    page = client.get("/chapters").get_data(as_text=True)
    assert "Check skip markers on" not in page
    assert "4 queued" in page


def test_skip_marker_check_marks_the_preview_and_hides_a_false_outro(
    media_env, monkeypatch
):
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, rows = _series_with_recaps(files)
    target = parts[-1]
    ending = [
        Segment(3560.0, 3563.0, "Stay tuned for scenes from our next episode."),
        Segment(3564.0, 3566.0, "Next time on Island Life,"),
        Segment(3566.0, 3590.0, "the merge changes everything."),
    ]
    first_minutes = [Segment(r["start"], r["end"], r["text"]) for r in rows]

    def fake_transcribe(
        _info, _model, _device, _dir, make_room=None, opening=False, window=None
    ):
        return (first_minutes if opening else ending), {}

    monkeypatch.setattr("curatarr.transcribe.transcribe", fake_transcribe)
    segments = {
        "jf-ep4": [
            {
                "Id": "music",
                "Type": "Credits",
                "Start": 3300.0,
                "End": 3311.0,
                "Source": "Chromaprint",
                "Suppressed": False,
            },
            {
                "Id": "real",
                "Type": "Credits",
                "Start": 3530.0,
                "End": 3590.0,
                "Source": "BlackFrame",
                "Suppressed": False,
            },
        ]
    }
    calls = []

    def plugin(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if method == "GET":
            return segments.get(path.split("/")[2], [])
        if method == "POST":
            return {"Id": f"ours-{kwargs['json']['Type']}"}
        return None

    arr.request = plugin
    enrichment.request(target, "recap")
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=target.id).one()
    assert (state.preview_start, state.preview_end) == (3559.5, 3600.0)
    assert state.preview_source == "runs to the end"
    assert (
        "POST",
        "/Episode/jf-ep4/Segments",
        {"Type": "Preview", "Start": 3559.5, "End": 3600.0},
    ) in calls
    assert ("DELETE", "/Episode/jf-ep4/Segments/music", None) in calls
    assert ("DELETE", "/Episode/jf-ep4/Segments/real", None) not in calls
    assert [h["id"] for h in state.markers_hidden] == ["music"]
    assert state.markers_checked_at is not None and state.recap_end == 82.0


def test_episode_page_shows_skip_markers_and_queues_a_check_first(
    app, client, media_env
):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    enrichment.queue_recaps()
    target = parts[-1]
    page = client.get(f"/chapters/parts/{target.id}").get_data(as_text=True)
    assert "Skip markers:" in page and "not checked yet" in page
    state = db.session.query(ChapterState).filter_by(media_part_id=target.id).one()
    state.job_state = "idle"
    db.session.commit()
    client.post(f"/chapters/parts/{target.id}/recap")
    db.session.expire_all()
    first = enrichment._due_jobs(1, scans=False, recaps=True)[0]
    assert first.media_part_id == target.id


def test_guessed_recap_on_an_episode_without_previously_on_is_hidden(
    media_env, monkeypatch
):
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, _rows = _series_with_recaps(files)
    cold_open = [
        Segment(0, 5, "Okay, who took the last donut?"),
        Segment(5, 9, "Not me."),
    ]
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe", lambda *_a, **_k: (cold_open, {})
    )
    calls = []

    def plugin(method, path, **kwargs):
        calls.append((method, path))
        if method == "GET":
            return [
                {
                    "Id": "cold",
                    "Type": "Recap",
                    "Start": 0,
                    "End": 92,
                    "Source": "Chapter",
                    "Suppressed": False,
                }
            ]
        return None

    arr.request = plugin
    enrichment.request(parts[-1], "recap")
    enrichment.dispatch(synchronous=True)
    assert ("DELETE", "/Episode/jf-ep4/Segments/cold") in calls


def test_lease_heartbeat_survives_a_busy_database(app, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from curatarr import leases

    owner = leases.acquire("chapters:busy-test", 600)
    calls = []

    def renew(scope, who, seconds):
        calls.append(scope)
        if len(calls) == 1:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return True

    monkeypatch.setattr(leases, "renew", renew)
    with leases.keep_alive("chapters:busy-test", owner, seconds=0.3) as lost:
        time.sleep(0.5)
    assert len(calls) >= 2 and not lost.is_set()


def test_review_page_lists_changes_and_undo_reverses_them(
    app, client, media_env, monkeypatch
):
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, rows = _series_with_recaps(files)
    target = parts[-1]
    monkeypatch.setattr(
        "curatarr.transcribe.transcribe",
        lambda *_a, **_k: (
            [Segment(r["start"], r["end"], r["text"]) for r in rows],
            {},
        ),
    )
    segments = {
        "jf-ep4": [
            {
                "Id": "guess",
                "Type": "Recap",
                "Start": 0,
                "End": 40,
                "Source": "Chapter",
                "Suppressed": False,
            }
        ]
    }
    calls = []

    def plugin(method, path, **kwargs):
        calls.append((method, path))
        if method == "GET":
            return segments.get(path.split("/")[2], [])
        if method == "POST" and path.endswith("/Segments"):
            return {"Id": "ours"}
        return None

    arr.request = plugin
    enrichment.request(target, "recap")
    enrichment.dispatch(synchronous=True)
    page = client.get("/chapters/markers").get_data(as_text=True)
    assert "Recap marked" in page and "Intro Skipper marker hidden" in page
    assert "replaced by Curatarr" in page
    state = db.session.query(ChapterState).filter_by(media_part_id=target.id).one()

    client.post(f"/chapters/markers/{state.id}/undo", data={"kind": "recap"})
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.recap_end is None and state.recap_source == "undone"
    assert ("DELETE", "/Episode/jf-ep4/Segments/ours") in calls
    chapters = files.files[target.file_path]["chapters"]
    assert chapters[0] == {"start": 0.0, "name": "Breakfast Plans"}  # chapter gone too

    client.post(
        f"/chapters/markers/{state.id}/undo",
        data={"kind": "hidden", "segment": "guess"},
    )
    db.session.expire_all()
    assert ("POST", "/Episode/jf-ep4/Segments/guess/Restore") in calls
    assert db.session.get(ChapterState, state.id).markers_hidden is None


def test_movies_keep_only_their_credits_marker(app, client, media_env):
    files, arr = media_env
    _library, _media, part = _movie()
    files.add(part.file_path)
    state = enrichment.request(part, "generate")  # a queued chapter job stays queued
    calls = []
    segments = [
        {
            "Id": "rec",
            "Type": "Recap",
            "Start": 0,
            "End": 92,
            "Source": "Chapter",
            "Suppressed": False,
        },
        {
            "Id": "int",
            "Type": "Introduction",
            "Start": 0,
            "End": 60,
            "Source": "Chapter",
            "Suppressed": False,
        },
        {
            "Id": "cred",
            "Type": "Credits",
            "Start": 6800,
            "End": 7100,
            "Source": "BlackFrame",
            "Suppressed": False,
        },
    ]

    def plugin(method, path, **kwargs):
        calls.append((method, path))
        return segments if method == "GET" else None

    arr.request = plugin
    assert enrichment.check_movie_markers() == 1
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert ("DELETE", "/Episode/jf-film/Segments/rec") in calls
    assert ("DELETE", "/Episode/jf-film/Segments/int") in calls
    assert ("DELETE", "/Episode/jf-film/Segments/cred") not in calls
    assert state.job_state == "queued" and state.requested_action == "generate"
    assert {h["id"] for h in state.markers_hidden} == {"rec", "int"}
    assert enrichment.check_movie_markers() == 0  # checked once


def test_identity_check_flags_an_episode_that_fits_another_summary(
    app, client, media_env, monkeypatch, identity_on
):
    import json

    from curatarr.chapters import Segment

    files, arr = media_env
    parts, _rows = _series_with_recaps(files)
    target = parts[-1]
    show = target.media
    show.sonarr_id = 42
    words = "Homer license driving Marge drives car walk license driving car " * 20
    full = [Segment(t, t + 5, words) for t in range(0, 3000, 30)]
    key = "f" * 32
    (enrichment.cache_dir() / "transcripts" / f"{key}.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"start": s.start, "end": s.end, "text": s.text} for s in full
                ],
                "details": {"language": "en"},
            }
        )
    )
    state = enrichment.state_for(target)
    state.transcript_key = key
    db.session.commit()
    summaries = [
        {"seasonNumber": 1, "episodeNumber": n, "title": t, "overview": o}
        for n, t, o in [
            (1, "Breakfast", "The castaways argue over rice and coconuts at sunrise."),
            (2, "Storm", "A storm flattens the shelter and the tribe rebuilds it."),
            (
                3,
                "License Lost",
                "Homer loses his driving license and Marge drives the car.",
            ),
            (4, "Idol Hunt", "Lena searches the swamp for a hidden immunity idol."),
            (5, "Merge", "The tribes merge and old alliances split apart."),
        ]
    ]
    arr.episodes = lambda series_id: summaries
    arr.request = lambda method, path, **kwargs: []
    enrichment._summaries_cache.clear()
    enrichment.request(target, "recap")
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.identity_issue["kind"] == "episode"
    assert (state.identity_issue["filed_as"], state.identity_issue["fits"]) == (4, 3)
    page = client.get("/chapters/identity").get_data(as_text=True)
    assert "fits S01E03" in page
    api = client.get("/api/v1/identity-flags").get_json()["flags"]
    assert [(f["filed_as"], f["fits"]) for f in api] == [(4, 3)]
    assert "Possible wrong episodes" in client.get("/chapters").get_data(as_text=True)
    client.post(f"/chapters/identity/{state.id}/dismiss")
    assert "Nothing flagged" in client.get("/chapters/identity").get_data(as_text=True)


def test_make_english_the_default_from_the_flag(
    app, client, media_env, monkeypatch, identity_on
):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    target = parts[-1]
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "eac3",
            "language": "jpn",
            "default": True,
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "eac3",
            "language": "eng",
            "default": False,
        },
    ]
    original_probe = files.probe

    def probe(path):
        info = original_probe(path)
        info.streams = [dict(s) for s in streams]
        return info

    def set_default(path, info, index):
        for s in streams:
            if s["codec_type"] == "audio":
                s["default"] = s["index"] == index

    monkeypatch.setattr(enrichment, "probe", probe)
    monkeypatch.setattr("curatarr.mediafile.set_default_audio", set_default)
    state = enrichment.state_for(target)
    state.identity_issue = {
        "kind": "audio",
        "language": "ja",
        "english_track": True,
        "sample": "…",
    }
    db.session.commit()
    assert "Make English the default" in client.get("/chapters/identity").get_data(
        as_text=True
    )
    client.post(f"/chapters/identity/{state.id}/fix-audio")
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert streams[2]["default"] and not streams[1]["default"]
    assert state.identity_issue["fixed"] and state.identity_checked_at is None
    assert state.job_state == "queued" and state.requested_action == "regenerate"


def test_identity_flags_api_reports_swaps_and_confirmed_targets(
    app, client, media_env, identity_on
):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    show = parts[0].media
    show.sonarr_id = 42
    first, second, third = (enrichment.state_for(p) for p in parts[:3])
    first.identity_issue = {
        "kind": "episode",
        "file": parts[0].file_path,
        "season": 1,
        "filed_as": 1,
        "fits": 2,
        "margin": 6.0,
    }
    second.identity_issue = {
        "kind": "episode",
        "file": parts[1].file_path,
        "season": 1,
        "filed_as": 2,
        "fits": 1,
        "margin": 5.0,
    }
    third.identity_checked_at = utcnow()
    db.session.commit()
    flags = {
        f["filed_as"]: f
        for f in client.get("/api/v1/identity-flags").get_json()["flags"]
    }
    assert flags[1]["swapped"] and flags[2]["swapped"]
    assert flags[1]["sonarr_series_id"] == 42 and flags[1]["fits"] == 2
    # Once the file is moved or replaced, the flag is stale and disappears.
    parts[0].file_path = "/tv/Island Life/S01E02.mkv"
    db.session.commit()
    flags = client.get("/api/v1/identity-flags").get_json()["flags"]
    assert [f["filed_as"] for f in flags] == [2]


def test_checks_needing_transcription_wait_for_the_chapter_backlog(media_env):
    _files, _arr = media_env
    _library, media, _part = _movie()
    states = []
    for n in range(4):
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=1,
            episode_number=n,
            has_file=True,
            file_path=f"/tv/e{n}.mkv",
        )
        db.session.add(part)
        db.session.flush()
        states.append(enrichment.state_for(part))
    chapter_job, transcribed, untranscribed, asked_for = states
    chapter_job.job_state, chapter_job.requested_action = "queued", "generate"
    for state in (transcribed, untranscribed, asked_for):
        state.job_state, state.requested_action = "queued", "recap"
        state.requested_at = utcnow()
    transcribed.transcript_key = "a" * 32
    asked_for.requested_at = utcnow() - timedelta(days=3650)  # from the episode's page
    db.session.commit()
    due = {s.id for s in enrichment._due_jobs(10, scans=False, recaps=True)}
    assert due == {transcribed.id, asked_for.id}
    chapter_job.job_state = "idle"
    db.session.commit()
    due = {s.id for s in enrichment._due_jobs(10, scans=False, recaps=True)}
    assert untranscribed.id in due


def test_identity_flags_follow_the_file_not_a_stale_identity(
    app, client, media_env, identity_on
):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    state = enrichment.state_for(parts[1])
    state.identity_issue = {
        "kind": "episode",
        "file": parts[1].file_path,
        "season": 1,
        "filed_as": 2,
        "fits": 3,
        "margin": 5.0,
    }
    state.identity_checked_at = utcnow()
    state.media_identity_id = "gone-since-discovery"
    db.session.commit()
    flags = client.get("/api/v1/identity-flags").get_json()["flags"]
    assert [(f["filed_as"], f["fits"], f["series_title"]) for f in flags] == [
        (2, 3, "Island Life")
    ]


def test_search_what_was_said(app, client, media_env):
    import json

    from curatarr import search

    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    lines = [
        {"start": 10.0, "end": 14.0, "text": "Has anyone seen my red stapler?"},
        {"start": 14.0, "end": 18.0, "text": "I was told I could keep it."},
        {"start": 300.0, "end": 305.0, "text": "Morning meeting starts at nine."},
    ]
    (enrichment.cache_dir() / "transcripts" / f"{'d' * 32}.json").write_text(
        json.dumps({"segments": lines, "details": {}})
    )
    state = enrichment.state_for(parts[-1])
    state.transcript_key = "d" * 32
    db.session.commit()
    search.index._checked = 0  # fresh index for the test
    results = client.get("/api/v1/transcripts/search?q=red stapler").get_json()[
        "results"
    ]
    assert results[0]["label"] == "Island Life · S01E04" and results[0]["exact"]
    assert results[0]["timestamp"] == "00:00:10"
    loose = client.get("/api/v1/transcripts/search?q=stapler+keep+told").get_json()[
        "results"
    ]
    assert loose and not loose[0]["exact"]
    assert (
        client.get("/api/v1/transcripts/search?q=spaceship").get_json()["results"] == []
    )
    page = client.get("/transcripts?q=red+stapler").get_data(as_text=True)
    assert "Has anyone seen my <mark>red</mark> <mark>stapler</mark>?" in page


def test_old_transcripts_get_linked_so_search_finds_them(app, client, media_env):
    import json

    from curatarr.transcribe import cache_key

    files, _arr = media_env
    _library, _media, part = _movie()
    files.add(part.file_path)
    state = enrichment.state_for(part)
    state.chapter_status = "generated_semantic"
    db.session.commit()
    key = cache_key(
        files.probe(part.file_path),
        enrichment.config_for(part.media.library_id)["semantic"]["model"],
    )
    (enrichment.cache_dir() / "transcripts").mkdir(parents=True, exist_ok=True)
    (enrichment.cache_dir() / "transcripts" / f"{key}.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 5, "end": 9, "text": "Mind the flying skillets!"}
                ],
                "details": {},
            }
        )
    )
    enrichment._link_tried.clear()
    assert enrichment.link_transcripts() == 1
    assert db.session.get(ChapterState, state.id).transcript_key == key
    assert enrichment.link_transcripts() == 0  # nothing left to try


def test_a_flag_survives_the_path_being_written_differently(
    app, client, media_env, identity_on
):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    state = enrichment.state_for(parts[1])
    state.identity_issue = {
        "kind": "episode",
        "file": "/mnt/media/Island Life/S01E02.mkv",
        "season": 1,
        "filed_as": 2,
        "fits": 3,
        "margin": 5.0,
    }
    state.identity_checked_at = utcnow()
    db.session.commit()
    assert parts[1].file_path == "/tv/Island Life/S01E02.mkv"
    assert len(client.get("/api/v1/identity-flags").get_json()["flags"]) == 1


def test_check_skip_markers_button_hands_the_queueing_to_the_worker(
    app, client, media_env, monkeypatch
):
    files, _arr = media_env
    _series_with_recaps(files)
    monkeypatch.setattr(enrichment, "QUEUE_BATCH", 2)  # several small commits
    client.post("/chapters/recaps")
    assert db.session.query(ChapterState).filter_by(job_state="queued").count() == 0
    assert enrichment.run_requested_skip_marker_check() == 4
    assert (
        db.session.query(ChapterState)
        .filter_by(job_state="queued", requested_action="recap")
        .count()
        == 4
    )
    assert enrichment.run_requested_skip_marker_check() == 0  # the request was used up


def test_a_cleared_flag_does_not_break_the_list(app, client, media_env, identity_on):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    state = enrichment.state_for(parts[0])
    state.identity_issue = {
        "kind": "episode",
        "season": 1,
        "filed_as": 1,
        "fits": 2,
        "margin": 5.0,
    }
    db.session.commit()
    state.identity_issue = None  # an update to None stores JSON null, not SQL NULL
    db.session.commit()
    assert client.get("/chapters/identity").status_code == 200
    assert client.get("/api/v1/identity-flags").get_json()["flags"] == []


def test_the_episode_check_is_off_by_default(app, client, media_env):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    state = enrichment.state_for(parts[0])
    state.identity_issue = {
        "kind": "episode",
        "season": 1,
        "filed_as": 1,
        "fits": 2,
        "margin": 5.0,
    }
    db.session.commit()
    assert not enrichment.identity_check_enabled()
    assert client.get("/api/v1/identity-flags").get_json()["flags"] == []
    assert "This check is off" in client.get("/chapters/identity").get_data(
        as_text=True
    )
    assert "Possible wrong episodes" not in client.get("/chapters").get_data(
        as_text=True
    )


def test_chapter_jobs_ceiling_shows_its_range_and_accepts_up_to_eight(app, client):
    page = client.get("/chapters/settings").get_data(as_text=True)
    assert "Jobs at once, at most (1–8)" in page and 'max="8"' in page
    form = {
        "chapter_mode": "smart",
        "minimum_duration_minutes": "20",
        "target_spacing_minutes": "7",
        "minimum_spacing_minutes": "4",
        "maximum_spacing_minutes": "10",
        "whisper_model": "medium",
        "fallback_format": "Chapter {number}",
        "fixed_intervals": "true",
        "concurrency": "6",
    }
    client.post("/chapters/settings", data=form)
    assert enrichment.options()["concurrency"] == 6
    client.post("/chapters/settings", data={**form, "concurrency": "9"})
    assert enrichment.options()["concurrency"] == 6  # over the ceiling: refused


def test_credits_people_talk_through_are_trimmed_and_can_be_undone(
    media_env, monkeypatch
):
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, rows = _series_with_recaps(files)
    target = parts[-1]
    talk = [
        Segment(t, t + 4.5, f"line {n} of the punishment, nobody has said this before")
        for n, t in enumerate(range(3000, 3400, 6))
    ]
    first_minutes = [Segment(r["start"], r["end"], r["text"]) for r in rows]

    def fake_transcribe(
        _info, _model, _device, _dir, make_room=None, opening=False, window=None
    ):
        return (first_minutes if opening else talk), {}

    monkeypatch.setattr("curatarr.transcribe.transcribe", fake_transcribe)
    segments = [
        {
            "Id": "long",
            "Type": "Credits",
            "Start": 3000.0,
            "End": 3590.0,
            "Source": "Chromaprint",
            "Suppressed": False,
        }
    ]
    calls = []

    def plugin(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if method == "GET":
            return segments if path.split("/")[2] == "jf-ep4" else []
        if method == "POST" and kwargs.get("json"):
            return {"Id": f"ours-{kwargs['json']['Type']}"}
        return None

    arr.request = plugin
    enrichment.request(target, "recap")
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.query(ChapterState).filter_by(media_part_id=target.id).one()
    added = [c for c in calls if c[0] == "POST" and c[2] and c[2]["Type"] == "Credits"]
    assert len(added) == 1 and 3400 < added[0][2]["Start"] < 3403
    assert added[0][2]["End"] == 3590.0
    assert ("DELETE", "/Episode/jf-ep4/Segments/long", None) in calls
    [entry] = state.markers_hidden
    assert (
        entry["replaced_by"] == "ours-Credits"
        and "credits now start there" in entry["reason"]
    )
    calls.clear()
    enrichment.undo_marker(state, "hidden", "long")
    assert ("DELETE", "/Episode/jf-ep4/Segments/ours-Credits", None) in calls
    assert ("POST", "/Episode/jf-ep4/Segments/long/Restore", None) in calls


def test_title_page_shows_what_curatarr_did_to_each_file(app, client, media_env):
    _library, media, part = _movie()
    state = enrichment.state_for(part)
    state.duration_seconds, state.chapter_status, state.chapter_count = (
        5400,
        "generated_semantic",
        9,
    )
    state.recap_start, state.recap_end = 0.0, 82.0
    state.markers_hidden = [{"id": "x", "type": "Credits", "reason": "test"}]
    state.dub_result = {"original": "fr", "verdict": "no_english"}
    db.session.commit()
    page = client.get(f"/titles/{media.id}").get_data(as_text=True)
    panel = page.split('id="enhance-heading"')[1].split("</section>")[0]
    assert "9 chapters" in panel and "Recap 0:00–1:22" in panel
    assert "1 marker fixed" in panel and "No English audio" in panel
    assert f"/chapters/parts/{part.id}" in panel


def test_failed_files_are_counted_and_listed_alike(client, media_env):
    """Including a file that couldn't be read at all, so it has no length."""
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.chapter_status, state.chapter_error = "failed", "The media file was not found"
    state.attempts = 3
    db.session.commit()
    assert enrichment.maintenance_failed_count() == 1
    assert enrichment.maintenance_summary()["failed"] == 1
    page = client.get("/chapters").text
    assert 'id="failed"' in page and "The media file was not found" in page


def test_failures_are_grouped_by_why(client, media_env):
    """So permanent failures aren't lost among the share being offline."""
    reasons = [
        "The media file was not found at its expected path",
        "The media file was not found at its expected path",
        "Writing chapters failed: [Errno 13] Permission denied",
        "Something nobody planned for",
    ]
    states = []
    _library, media, first = _movie()
    for i, reason in enumerate(reasons):
        part = first
        if i:
            part = MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                file_path=f"/movies/Film (2020)/Film {i}.mkv",
            )
            db.session.add(part)
            db.session.flush()
        state = enrichment.state_for(part)
        state.chapter_status, state.chapter_error = "failed", reason
        states.append(state)
    db.session.commit()
    groups = enrichment.failed_groups()
    assert (groups[0]["kind"], groups[0]["count"]) == ("missing", 2)  # biggest first
    assert {(g["kind"], g["count"]) for g in groups[1:]} == {("write", 1), ("other", 1)}
    page = client.get("/chapters").text
    assert (
        "Couldn&#39;t find the file" in page
        and "Couldn&#39;t write to the file" in page
    )
    assert "Try these 2 again" in page
    client.post("/chapters/failed/retry-kind/write")
    db.session.expire_all()
    assert [db.session.get(ChapterState, s.id).job_state for s in states] == [
        "idle",
        "idle",
        "queued",
        "idle",
    ]


def _queued_missing(arr, files, movie_record):
    _library, _media, part = _movie()
    _navigation(_library)
    arr.movie_record = movie_record
    state = enrichment.request(part, "generate")
    return part, state


def test_a_file_radarr_also_lost_is_not_a_failure(media_env):
    """Replaced for having no English audio: wait for the next copy, quietly."""
    files, arr = media_env
    part, state = _queued_missing(arr, files, {"id": 7, "hasFile": False})
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.chapter_status == "not_processed" and state.job_state == "idle"
    assert (
        not state.chapter_error and db.session.get(MediaPart, part.id).has_file is False
    )
    assert enrichment.maintenance_failed_count() == 0


def test_a_renamed_file_is_followed(media_env):
    files, arr = media_env
    part, state = _queued_missing(
        arr,
        files,
        {
            "id": 7,
            "hasFile": True,
            "movieFile": {"path": "/movies/Film (2020)/Film.v2.mkv"},
        },
    )
    files.add("/movies/Film (2020)/Film.v2.mkv")
    enrichment.dispatch(synchronous=True)
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert db.session.get(MediaPart, part.id).file_path.endswith("Film.v2.mkv")
    assert state.chapter_status != "failed"


def test_an_unexplained_missing_file_fails_after_a_day_and_has_controls(
    client, media_env
):
    files, arr = media_env
    _part, state = _queued_missing(arr, files, None)  # Radarr can't be asked
    state.attempts = enrichment.MISSING_PATIENCE  # a day of retries already
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.chapter_status == "failed"
    page = client.get("/chapters").text
    assert "Find another copy" in page and "Try again" in page and "Ignore" in page
    arr.movie_record = {"id": 7, "hasFile": False}
    client.post(f"/chapters/failed/{state.id}/search")
    assert arr.searched == (7,)
    client.post(f"/chapters/failed/{state.id}/ignore")
    assert enrichment.maintenance_failed_count() == 0


def test_old_missing_file_failures_are_cleared_with_radarr(media_env):
    files, arr = media_env
    _part, state = _queued_missing(arr, files, None)
    state.attempts = enrichment.MISSING_PATIENCE
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    assert enrichment.maintenance_failed_count() == 1
    arr.movie_record = {"id": 7, "hasFile": False}
    enrichment._missing_tried.clear()
    assert enrichment.resolve_missing_failures() == 1
    assert enrichment.maintenance_failed_count() == 0


def test_a_file_on_an_offline_share_waits_instead_of_failing(client, media_env):
    """Radarr still has it: the network share dropped. Retry; fail after a day."""
    files, arr = media_env
    _part, state = _queued_missing(
        arr,
        files,
        {
            "id": 7,
            "hasFile": True,
            "movieFile": {"path": "/movies/Film (2020)/Film.mkv"},
        },
    )
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.chapter_status != "failed" and state.job_state == "queued"
    assert state.chapter_error == enrichment.UNREACHABLE and state.next_attempt_at
    arr.movie_record = None  # Radarr unreachable too (the whole network is down)
    state.next_attempt_at = None
    db.session.commit()
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    assert db.session.get(ChapterState, state.id).chapter_status != "failed"


def test_not_found_failures_heal_when_the_share_returns(client, media_env):
    files, _arr = media_env
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.chapter_status, state.chapter_error = (
        "failed",
        "The media file was not found at its expected path",
    )
    state.subtitle_status, state.subtitle_error = (
        "failed",
        "The media file was not found",
    )
    db.session.commit()
    files.add(part.file_path)  # the share is back
    enrichment._missing_tried.clear()
    assert enrichment.resolve_missing_failures() == 1
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert (state.job_state, state.requested_action, state.subtitle_status) == (
        "queued",
        "auto",
        None,
    )
    assert enrichment.maintenance_failed_count() == 0


def test_try_all_again(client, media_env):
    _files, _arr = media_env
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.chapter_status, state.chapter_error = "failed", "Something broke"
    db.session.commit()
    assert "Try all 1 again" in client.get("/chapters").text
    client.post("/chapters/failed/retry-all")
    db.session.expire_all()
    assert db.session.get(ChapterState, state.id).job_state == "queued"


def test_try_all_again_includes_failed_files_queued_for_other_work(client, media_env):
    """A failed file waiting for a marker check still counts as failed; the
    button has to clear it, or the badge never goes down."""
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.chapter_status, state.chapter_error = "failed", "Something broke"
    state.job_state, state.requested_action = "queued", "recap"
    db.session.commit()
    assert enrichment.maintenance_failed_count() == 1
    client.post("/chapters/failed/retry-all")
    db.session.expire_all()
    assert enrichment.maintenance_failed_count() == 0


def test_an_unreachable_file_waits_without_counting_as_failed(media_env):
    """An old "not found" failure on a share that's still offline waits."""
    _files, arr = media_env
    _library, _media, part = _movie()
    arr.movie_record = None  # Radarr can't be asked either
    state = enrichment.state_for(part)
    state.chapter_status = "failed"
    state.chapter_error = "The media file was not found at its expected path"
    db.session.commit()
    enrichment._missing_tried.clear()
    enrichment.resolve_missing_failures()
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert (state.job_state, state.requested_action) == ("queued", "auto")
    assert state.chapter_error == enrichment.UNREACHABLE
    assert enrichment.maintenance_failed_count() == 0


def test_checks_that_hit_an_offline_share_are_redone(media_env):
    _library, _media, part = _movie()
    state = enrichment.state_for(part)
    state.dub_checked_at = state.file_checked_at = utcnow()
    state.dub_result = {
        "original": "fr",
        "error": "[Errno 112] Host is down: '/movies/x.mkv'",
    }
    state.file_check = {"issues": [], "error": "The file wasn't found"}
    db.session.commit()
    enrichment._last_recheck[0] = utcnow() - timedelta(days=1)
    enrichment.resolve_missing_failures()
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.dub_checked_at is None and state.file_checked_at is None


def test_radarr_is_never_asked_while_changes_are_unsaved(app, media_env, monkeypatch):
    """Unsaved changes hold SQLite's write lock; the web app would wait on it."""
    _files, arr = media_env
    seen = []
    _library, media, first = _movie()
    for n in range(3):
        part = first
        if n:
            part = MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                file_path=f"/movies/Film (2020)/Film.{n}.mkv",
            )
            db.session.add(part)
            db.session.flush()
        state = enrichment.state_for(part)
        state.chapter_status = "failed"
        state.chapter_error = "The media file was not found at its expected path"
    db.session.commit()
    real = enrichment._arr_file

    def watched(part, media):
        seen.append(not database_free(app))
        return real(part, media)

    monkeypatch.setattr(enrichment, "_arr_file", watched)
    arr.movie_record = {"id": 7, "hasFile": False}
    enrichment._missing_tried.clear()
    assert enrichment.resolve_missing_failures() == 3
    assert seen == [False, False, False]


@pytest.mark.parametrize("action", ["generate", "recap"])
def test_slow_episode_work_never_holds_the_write_lock(
    app, media_env, monkeypatch, action
):
    """Transcription, Ollama and Intro Skipper can take minutes: with a change
    left unsaved, SQLite's write lock is held and every other job fails."""
    from curatarr.chapters import Segment

    files, arr = media_env
    parts, rows = _series_with_recaps(files)
    target = parts[-1]
    files.files[target.file_path]["chapters"] = []
    segments = [Segment(r["start"], r["end"], r["text"]) for r in rows] + [
        Segment(t, t + 15, f"line {t}") for t in range(120, 3600, 20)
    ]
    seen = []

    def transcribe(*_a, **_k):
        seen.append(("transcribe", database_free(app)))
        return segments, {"cached": False, "device": "cpu"}

    def ask_jellyfin(method, path, **kwargs):
        seen.append((f"{method} {path}", database_free(app)))
        return {"Id": "ours"} if method == "POST" else []

    class Model:
        model = "fake"
        calls = 0

        def ask(self, prompt, schema):
            seen.append(("model", database_free(app)))
            self.calls += 1
            if "choice" in schema["properties"]:
                return {"choice": "A"}
            return {"summary": "Something happens.", "name": f"Scene {self.calls}"}

    monkeypatch.setattr("curatarr.transcribe.transcribe", transcribe)
    arr.request = ask_jellyfin
    enrichment.request(target, action)
    enrichment.dispatch(synchronous=True, llm=Model())
    assert seen
    assert [what for what, free in seen if not free] == []


def test_skip_marker_checks_use_every_free_slot(app, media_env, monkeypatch):
    """With few chapter jobs and nobody watching, skip-marker checks shouldn't
    be held to one slot while the rest sit idle."""
    from curatarr import adaptive

    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    for part in parts:
        enrichment.request(part, "recap")
    monkeypatch.setattr(enrichment, "RECAP_BATCH", 1)
    monkeypatch.setattr(
        adaptive.controller,
        "decide",
        lambda *a, **k: adaptive.Decision(3, False, 0.85, "test"),
    )
    monkeypatch.setattr(enrichment, "playback_sessions", list)
    started = {}
    monkeypatch.setattr(
        enrichment, "_start", lambda key, app, ids, llm: started.update({key: ids})
    )
    enrichment._threads.clear()
    enrichment.dispatch()
    batches = [ids for key, ids in started.items() if key.startswith("recap")]
    assert len(batches) == 3  # every free slot, not just one
    flat = [i for ids in batches for i in ids]
    assert len(flat) == len(set(flat))  # no episode in two batches


def test_a_film_radarr_no_longer_has_closes_instead_of_waiting(media_env, monkeypatch):
    """Radarr answering 404 means the film was removed, not that the share is
    offline: the job closes rather than waiting a day."""
    files, arr = media_env
    part, state = _queued_missing(arr, files, {"id": 7})

    def gone(movie_id):
        raise IntegrationError("Not found", status_code=404)

    monkeypatch.setattr(arr, "movie", gone)
    enrichment.dispatch(synchronous=True)
    db.session.expire_all()
    state = db.session.get(ChapterState, state.id)
    assert state.job_state == "idle" and state.chapter_error is None
    assert db.session.get(MediaPart, part.id).has_file is False


def test_a_chapter_job_waiting_to_retry_doesnt_hold_back_checks(media_env, monkeypatch):
    """A job waiting on an unreachable file counted as a chapter backlog, so
    checks that need a transcript never started while it waited."""
    files, arr = media_env
    _part, waiting = _queued_missing(arr, files, None)
    waiting.next_attempt_at = utcnow() + timedelta(minutes=10)
    db.session.commit()
    assert not enrichment._chapter_backlog()
    waiting.next_attempt_at = None
    db.session.commit()
    assert enrichment._chapter_backlog()


def test_scan_reads_only_new_files_unless_asked_for_all(app, client, media_env):
    files, _arr = media_env
    _library, _media, part = _movie()
    files.add(part.file_path)
    assert enrichment.queue_scan() == 1
    enrichment.dispatch(synchronous=True)
    page = client.get("/chapters").get_data(as_text=True)
    assert "new file" not in page and "Rescan all 1 files" in page
    assert enrichment.queue_scan() == 0  # already scanned
    client.post("/chapters/scan", data={"all": "1"})
    db.session.expire_all()
    assert db.session.query(ChapterState).one().job_state == "queued"


def test_skip_marker_checks_have_a_progress_bar(app, client, media_env):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    for part in parts:
        enrichment.request(part, "recap")
    first = db.session.query(ChapterState).filter_by(media_part_id=parts[0].id).one()
    first.job_state, first.requested_action = "idle", None
    first.markers_checked_at = utcnow()
    db.session.commit()
    forecast = enrichment.marker_forecast()
    assert forecast["done"] == 1 and forecast["remaining"] == len(parts) - 1
    page = client.get("/chapters").get_data(as_text=True)
    assert "skip-marker checks done since" in page


def test_overview_shows_the_skip_marker_progress(app, client, media_env):
    files, _arr = media_env
    parts, _rows = _series_with_recaps(files)
    for part in parts:
        enrichment.request(part, "recap")
    for part in parts:
        enrichment.state_for(part).duration_seconds = 1800.0  # scanned
    db.session.commit()
    page = client.get("/").get_data(as_text=True)
    care = page.split('id="care-heading"')[1].split("</section>")[0]
    assert f"{len(parts)} skip-marker checks to go" in care
