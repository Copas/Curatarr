"""One overall progress bar: items across every kind of work, finishing when
the slowest does."""

from datetime import timedelta

from curatarr import compat_audio, enrichment, progress, seek_repair
from curatarr.models import utcnow


def test_overall_takes_the_slowest_finish(app, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(
        enrichment,
        "job_forecast",
        lambda now=None: {
            "done": 10,
            "remaining": 2,
            "finishes_at": now + timedelta(minutes=10),
        },
    )
    monkeypatch.setattr(
        enrichment,
        "marker_forecast",
        lambda now=None: {
            "done": 30,
            "remaining": 60,
            "finishes_at": now + timedelta(hours=20),
        },
    )
    monkeypatch.setattr(compat_audio, "progress", lambda: None)
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    result = progress.overall(now)
    assert result["done"] == 40 and result["left"] == 62
    assert result["slowest"] == "Skip-marker checks" and result["time_left"] == "20 h"
    assert not result["approximate"] and result["unknown"] == []


def test_nothing_to_do_means_no_bar(app, monkeypatch):
    monkeypatch.setattr(enrichment, "job_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "marker_forecast", lambda now=None: None)
    monkeypatch.setattr(compat_audio, "progress", lambda: None)
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    assert progress.overall() is None


def test_rebuilds_without_a_pace_are_named(app, monkeypatch):
    monkeypatch.setattr(enrichment, "job_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "marker_forecast", lambda now=None: None)
    monkeypatch.setattr(
        compat_audio,
        "progress",
        lambda: {
            "on": True,
            "done": 5,
            "films": {"left": 1},
            "episodes": {"left": 4},
        },
    )
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    result = progress.overall()
    assert result["left"] == 5 and result["unknown"] == ["Playable audio"]
    assert result["time_left"] is None


def test_overview_puts_ongoing_work_at_the_top(app, client, monkeypatch):
    from test_enrichment import _movie

    from curatarr import db

    _movie("/movies/Film (2020)/Film.mkv")
    from curatarr.models import MediaPart

    enrichment.state_for(db.session.query(MediaPart).one()).duration_seconds = 60.0
    db.session.commit()
    monkeypatch.setattr(
        progress,
        "overall",
        lambda now=None: {
            "done": 1,
            "left": 2,
            "percent": 33.3,
            "finishes_at": utcnow() + timedelta(hours=1),
            "time_left": "1 h",
            "slowest": "Skip-marker checks",
            "approximate": False,
            "unknown": [],
            "parts": [],
        },
    )
    page = client.get("/").get_data(as_text=True)
    assert "Everything Curatarr is working through" in page
    assert page.index("Everything Curatarr is working through") < page.index(
        'id="needs-heading"'
    )


def test_rebuilds_share_one_estimate_from_their_joint_pace(app, monkeypatch):
    from curatarr import db
    from curatarr.models import LifecycleAction

    now = utcnow()
    for n in range(24):  # one rebuild an hour over the last day, all seek repairs
        db.session.add(
            LifecycleAction(
                idempotency_key=f"k{n}",
                action_type="seek_index_repaired",
                state="SUCCEEDED",
                reason_text="Rebuilt",
                created_at=now - timedelta(hours=n, minutes=1),
            )
        )
    db.session.commit()
    monkeypatch.setattr(enrichment, "job_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "marker_forecast", lambda now=None: None)
    monkeypatch.setattr(
        compat_audio,
        "progress",
        lambda: {"on": True, "done": 1, "films": {"left": 0}, "episodes": {"left": 4}},
    )
    monkeypatch.setattr(
        seek_repair,
        "progress",
        lambda: {"on": True, "checked": 100, "rebuilt": 10, "left": 60},
    )
    result = progress.overall(now)
    # 4 audio rebuilds + 60 files x 10% needing one = 10 rebuilds at 1 an hour.
    assert result["time_left"] == "10 h" and result["approximate"]
    assert result["unknown"] == []


def test_subtitle_jobs_are_part_of_the_bar(app, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(enrichment, "job_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "marker_forecast", lambda now=None: None)
    monkeypatch.setattr(
        enrichment,
        "subtitle_forecast",
        lambda now=None: {
            "done": 100,
            "remaining": 1500,
            "finishes_at": now + timedelta(hours=30),
        },
    )
    monkeypatch.setattr(compat_audio, "progress", lambda: None)
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    result = progress.overall(now)
    assert result["left"] == 1500 and result["slowest"] == "Subtitles"


def test_overview_counts_every_title_leaving_soon(app, client):
    from curatarr import db
    from curatarr.models import Library, MediaIdentity, PurgeCandidate

    library = Library(jellyfin_library_id="ls", name="Films", media_type="movies")
    db.session.add(library)
    db.session.flush()
    for number in range(7):  # more than Overview's upcoming list shows
        media = MediaIdentity(
            library_id=library.id, media_type="movie", title=f"Film {number}"
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            PurgeCandidate(
                media_identity_id=media.id,
                state="LEAVING_SOON",
                reason_code="requested",
                reason_text="Asked",
                reclaimable_bytes=1,
                scheduled_delete_at=utcnow() + timedelta(days=number + 1),
            )
        )
    db.session.commit()
    page = client.get("/").text
    assert "7 titles" in page and "Leaving soon</h2>" in page


def test_a_paused_hour_does_not_set_the_pace():
    now = utcnow()
    done = {1: 3, 6: 380, 24: 507}  # watching paused most of the last hour

    def finished_since(moment):
        hours = round((now - moment).total_seconds() / 3600)
        return done[hours]

    rate = enrichment._recent_pace(finished_since, now - timedelta(days=2), now)
    assert round(rate) == 63  # the six hours' pace, not 3 an hour


def test_chapter_quality_english_audio_and_subtitle_tracks_are_counted(
    app, client, monkeypatch
):
    from test_enrichment import _movie

    from curatarr import db, default_audio, subtitle_tracks
    from curatarr.models import ChapterState, MediaPart
    from curatarr.services import set_setting

    _movie("/movies/Film (2020)/Film.mkv")
    part = db.session.query(MediaPart).one()
    state = enrichment.state_for(part)
    state.chapter_status = "existing"  # scanned: Overview shows Library care
    state.duration_seconds = 60.0
    state.default_audio = {"at": "then", "listen": True}
    state.default_audio_size = part.size_bytes
    set_setting("chapter_options", {"subtitle_tracks": True})
    set_setting(default_audio.REMAINING_KEY, {"files": 4})
    set_setting(subtitle_tracks.REMAINING_KEY, {"files": 7})
    db.session.commit()
    monkeypatch.setattr(enrichment, "job_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "marker_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "subtitle_forecast", lambda now=None: None)
    monkeypatch.setattr(compat_audio, "progress", lambda: None)
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    monkeypatch.setattr(
        enrichment,
        "chapter_quality_progress",
        lambda: {"checked": 90, "left": 10, "total": 100, "per_hour": 100},
    )
    result = progress.overall()
    parts = {p["name"]: p for p in result["parts"]}
    assert parts["Chapter quality checks"]["left"] == 10
    assert parts["Chapter quality checks"]["finishes_at"]
    assert parts["English audio"]["left"] == 5  # four to look at, one to hear
    assert parts["Subtitle tracks"]["left"] == 7
    assert result["left"] == 22

    english = default_audio.progress()
    assert english["waiting"] == 1 and english["to_look"] == 4
    state = db.session.query(ChapterState).one()
    # Counted from History: the file's own record is replaced when it's looked
    # at again, and a file changed twice counts once.
    from curatarr.services import audit

    for n in (1, 2):
        audit(
            "default_audio_fixed",
            part.media_identity_id,
            "Film: its English track, heard to be English, is first and the default now.",
            f"default-audio-first:{state.id}:{n}",
            payload={"file": part.file_path},
        )
    state.default_audio = {"at": "now"}
    db.session.commit()
    english = default_audio.progress()
    assert english["fixed"] == 1 and english["checked"] == 1

    for address in ("/chapters", "/"):
        page = client.get(address).text
        assert "Chapter quality checks" in page, address
        assert "English audio" in page
        assert "Subtitle tracks in other languages" in page


def test_quality_progress_only_when_a_library_has_it_on(app):
    from test_enrichment import _movie

    from curatarr import db
    from curatarr.models import MediaPart

    _movie("/movies/Film (2020)/Film.mkv")
    assert enrichment.chapter_quality_progress() is None
    library = db.session.query(MediaPart).one().media.library
    from curatarr.services import set_setting

    set_setting("chapter_settings", {"enabled": True, "replace_poor": True})
    db.session.commit()
    state = enrichment.state_for(db.session.query(MediaPart).one())
    state.chapter_status, state.chapter_source = "existing", "embedded"
    db.session.commit()
    assert library.managed
    result = enrichment.chapter_quality_progress()
    assert result["left"] == 1 and result["checked"] == 0
