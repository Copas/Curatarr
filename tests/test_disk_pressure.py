from curatarr import db
from curatarr.lifecycle import _disk_pressure
from curatarr.models import Library, MediaIdentity
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
