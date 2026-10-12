"""Chapter jobs (curatarr/enrichment.py) with fake media tools and integrations."""

import time
from datetime import timedelta

import pytest

from curatarr import db, enrichment
from curatarr.chapters import Chapter
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


def test_chapter_work_pauses_while_someone_watches(app, client, media_env):
    from curatarr import throttle

    files, arr = media_env
    library, _media, part = _movie()
    files.add(part.file_path)
    _navigation(library)
    enrichment.request(part, "generate")
    arr.sessions_list = [{"NowPlayingItem": {"Id": "something-else"}}]
    assert enrichment.dispatch(synchronous=True) == 0
    assert throttle.paused() and files.writes == []
    assert "because someone is watching" in client.get("/chapters").get_data(
        as_text=True
    )
    arr.sessions_list = [{"DeviceName": "idle client"}]
    assert enrichment.dispatch(synchronous=True) == 1
    assert not throttle.paused() and len(files.writes) == 1
    assert "because someone is watching" not in client.get("/chapters").get_data(
        as_text=True
    )


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
        "every library with chapters enabled" in confirm and "Queue 1 files" in confirm
    )
    client.post("/chapters/libraries/all/process", data={"confirm": "1"})
    db.session.expire_all()
    queued = db.session.query(ChapterState).filter_by(job_state="queued").all()
    assert [state.media_part_id for state in queued] == [part.id]


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
