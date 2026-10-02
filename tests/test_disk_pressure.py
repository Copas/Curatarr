from datetime import timedelta

import pytest

from curatarr import db
from curatarr.lifecycle import _disk_pressure, _validate_deletion, evaluate_retention
from curatarr.models import (
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    utcnow,
)
from curatarr.policy import effective_policy, validate_policy


def test_disk_pressure_requires_explicit_matching_path(app, monkeypatch):
    class FakeArr:
        def diskspace(self):
            return [
                {"path": "/media", "totalSpace": 1000, "freeSpace": 100},
                {"path": "/media/movies", "totalSpace": 2000, "freeSpace": 100},
            ]

    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: FakeArr())
    with app.app_context():
        library = Library(
            jellyfin_library_id="pressure-demo", name="Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id, media_type="movie", title="Sample Film"
        )
        db.session.add(media)
        db.session.flush()
        policy, _ = effective_policy(
            "movie",
            library_values={
                "free_space_enabled": True,
                "disk_path": "/media/movies/library",
            },
        )
        assert _disk_pressure(media, policy) == (200, "critical")
        policy["disk_path"] = "/unmatched"
        assert _disk_pressure(media, policy) is None
        try:
            validate_policy({"free_space_enabled": True})
        except ValueError:
            pass
        else:
            raise AssertionError("Free-space policy without path must fail")


class PressureServices:
    def __init__(self, free):
        self.free = free
        self.queue_rows = []

    def queue(self):
        return {"records": self.queue_rows}

    def diskspace(self):
        return [
            {"path": "/media", "totalSpace": 1000, "freeSpace": self.free},
            {"path": "/other", "totalSpace": 1000, "freeSpace": 0},
        ]

    def request(self, _method, path):
        return {"tmdbId": 100 + int(path.split("/")[-1])}

    def item(self, item_id):
        return {"ProviderIds": {"Tmdb": str(100 + int(item_id.split("-")[-1]))}}


def _pressure_library(*, count=3, sizes=None):
    library = Library(
        jellyfin_library_id="pressure-movies", name="Movies", media_type="movies"
    )
    db.session.add(library)
    db.session.flush()
    db.session.add(
        LibraryPolicy(
            library_id=library.id,
            policy_json={
                "free_space_enabled": True,
                "disk_path": "/media/movies",
                "low_free_percent": 20,
                "critical_free_percent": 10,
                "low_pressure_review_mode": "require_review",
                "critical_pressure_review_mode": "automatic",
            },
        )
    )
    for index in range(1, count + 1):
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Film {index}",
            jellyfin_id=f"movie-{index}",
            radarr_id=index,
            tmdb_id=100 + index,
            added_at=utcnow() - timedelta(days=40),
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=(sizes or [60] * count)[index - 1],
            )
        )
    db.session.commit()
    return library


@pytest.mark.parametrize(
    ("free", "expected_count", "expected_state", "expected_level"),
    [
        (250, 0, None, None),
        (150, 1, "REVIEW", "low"),
        (50, 3, "LEAVING_SOON", "critical"),
    ],
)
def test_pressure_levels_select_only_needed_media(
    app, monkeypatch, free, expected_count, expected_state, expected_level
):
    services = PressureServices(free)
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: services)
    with app.app_context():
        _pressure_library()
        assert evaluate_retention() == expected_count
        candidates = db.session.query(PurgeCandidate).all()
        assert len(candidates) == expected_count
        assert all(row.state == expected_state for row in candidates)
        assert all(
            row.score_detail_json["pressure_level"] == expected_level
            for row in candidates
        )


def test_pressure_records_deficit_when_eligible_media_cannot_reach_target(
    app, monkeypatch
):
    services = PressureServices(50)
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: services)
    with app.app_context():
        _pressure_library(count=2, sizes=[30, 30])
        assert evaluate_retention() == 2
        deficit = (
            db.session.query(LifecycleAction)
            .filter_by(action_type="capacity_deficit")
            .one()
        )
        assert "90 bytes" in deficit.reason_text


def test_pressure_candidate_blocks_when_ranking_changes(app, monkeypatch):
    services = PressureServices(150)
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: services)
    with app.app_context():
        _pressure_library(count=2)
        assert evaluate_retention() == 1
        candidate = db.session.query(PurgeCandidate).one()
        candidate.state = "APPROVED"
        db.session.commit()
        assert _validate_deletion(candidate) is None
        services.queue_rows = [{"movieId": candidate.media.radarr_id}]
        assert _validate_deletion(candidate) == "Download or import is active"


def test_pressure_candidate_blocks_after_pressure_clears(app, monkeypatch):
    services = PressureServices(150)
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: services)
    with app.app_context():
        _pressure_library(count=2)
        assert evaluate_retention() == 1
        candidate = db.session.query(PurgeCandidate).one()
        candidate.state = "APPROVED"
        db.session.commit()
        services.free = 250
        assert _validate_deletion(candidate) == "Disk pressure is no longer confirmed"


@pytest.mark.parametrize(
    "bad_disks",
    [
        None,
        {"path": "/media"},
        [{"path": "/media", "totalSpace": "1000", "freeSpace": 1}],
        [{"path": "/media", "totalSpace": 1000, "freeSpace": float("nan")}],
    ],
)
def test_invalid_disk_response_never_triggers_deletion(app, monkeypatch, bad_disks):
    services = PressureServices(150)
    services.diskspace = lambda: bad_disks
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: services)
    with app.app_context():
        _pressure_library(count=1)
        assert evaluate_retention() == 0


def test_invalid_queue_response_blocks_candidate(app, monkeypatch):
    services = PressureServices(150)
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: services)
    with app.app_context():
        _pressure_library(count=1)
        assert evaluate_retention() == 1
        candidate = db.session.query(PurgeCandidate).one()
        candidate.state = "APPROVED"
        db.session.commit()
        services.queue = lambda: {"records": [{"movieId": 1}, "bad-row"]}
        assert (
            _validate_deletion(candidate) == "External integration cannot be verified"
        )
