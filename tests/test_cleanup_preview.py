import os
from datetime import timedelta

from curatarr import db
from curatarr.models import (
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    utcnow,
)


class NasNotReported:
    def diskspace(self):
        return [{"path": "/", "totalSpace": 10_000, "freeSpace": 9_000}]

    def queue(self):
        return {"records": []}


def _setup(monkeypatch, tmp_path):
    class Stats:  # 1000-byte disk, 600 free (60%): no real pressure
        f_blocks, f_frsize, f_bavail = 1000, 1, 600

    real = os.statvfs
    monkeypatch.setattr(
        "os.statvfs", lambda p: Stats() if str(p).startswith(str(tmp_path)) else real(p)
    )
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: NasNotReported())
    library = Library(jellyfin_library_id="m", name="Movies", media_type="movies")
    db.session.add(library)
    db.session.flush()
    for index in range(1, 5):
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Film {index}",
            jellyfin_id=f"film-{index}",
            radarr_id=index,
            tmdb_id=index,
            added_at=utcnow() - timedelta(days=500 - index * 10),
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=40,
            )
        )
    db.session.commit()


def test_preview_shows_only_enough_and_changes_nothing(
    app, client, monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    # Pretend 10% free: 50 bytes are needed to get back to 15%, so two 40s.
    page = client.get(f"/cleanup-preview?free=10&path={tmp_path}").text
    assert "2 would be picked and marked Leaving Soon" in page
    assert page.index(">Film 1<") < page.index(">Film 2<")  # oldest first
    assert ">Film 3<" not in page
    assert db.session.query(PurgeCandidate).count() == 0
    assert db.session.query(LifecycleAction).count() == 0


def test_preview_reports_when_nothing_would_be_picked(
    app, client, monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    page = client.get(f"/cleanup-preview?free=40&path={tmp_path}").text
    assert "nothing would be picked" in page


def test_preview_flags_disks_it_cannot_measure(app, client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    page = client.get("/cleanup-preview?free=10&path=/no/such/mount").text
    assert "Could not measure the disk for: Movies" in page


def test_preview_page_sits_under_retention(app, client):
    page = client.get("/cleanup-preview").text
    assert 'href="/rules/retention" aria-current="page">Retention<' in page
    assert "Preview" in page
    retention = client.get("/rules/retention").text
    assert "Preview what free-space cleanup would pick" in retention
