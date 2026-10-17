"""Shrink instead of delete: swap a big film for a much smaller copy."""

from datetime import timedelta

from test_curation import _film

from curatarr import db, shrink
from curatarr.models import PurgeCandidate, utcnow

GB = 1024**3


def _release(title, gb, height=1080, rejections=()):
    return {
        "guid": title,
        "indexerId": 1,
        "title": title,
        "size": int(gb * GB),
        "quality": {"quality": {"resolution": height}},
        "rejections": list(rejections),
    }


class Radarr:
    def __init__(self, releases, *, upgrades=False):
        self.releases_found = releases
        self.upgrades = upgrades
        self.calls = []
        self.has_file = True

    def movie(self, movie_id):
        return {
            "id": movie_id,
            "monitored": True,
            "hasFile": self.has_file,
            "qualityProfileId": 4,
            "movieFile": {
                "id": 9,
                "size": 20 * GB,
                "quality": {"quality": {"resolution": 1080}},
            },
        }

    def releases(self, movie_id):
        self.calls.append("search")
        return self.releases_found

    def quality_profile(self, profile_id):
        return {"upgradeAllowed": self.upgrades}

    def set_monitored(self, movie_id, monitored):
        self.calls.append(f"monitored={monitored}")

    def delete_movie_file(self, file_id):
        self.calls.append(f"delete {file_id}")

    def grab(self, release):
        self.calls.append(f"grab {release['title']}")

    def search_movie(self, movie_id):
        self.calls.append("search any")


def test_the_best_copy_half_the_size_or_smaller_is_picked():
    found = shrink.pick(
        [
            _release("Big.1080p", 15),
            _release("Small.720p", 3, height=720),
            _release("Good.1080p", 8),
            _release("Better.1080p", 9.5),
            _release("Part1.1080p", 5).copy() | {"title": "Film.Part.1.1080p"},
            _release("Blocked.1080p", 6, rejections=["Release is blocklisted"]),
            _release("Sample", 0.2),
        ],
        20 * GB,
        2160,
    )
    assert found["title"] == "Better.1080p"
    assert shrink.pick([_release("Only.720p", 4, height=720)], 20 * GB, 1080) is None


def test_shrink_swaps_only_when_a_smaller_copy_exists(app, monkeypatch):
    media = _film("Epic", size=20 * GB)
    candidate = PurgeCandidate(
        media_identity_id=media.id,
        state="REVIEW",
        reason_code="disk_pressure",
        reason_text="Disk pressure is low.",
        reclaimable_bytes=20 * GB,
    )
    db.session.add(candidate)
    db.session.commit()
    radarr = Radarr(
        [_release("Epic.1080p.WEB", 7, rejections=["Existing file meets cutoff"])]
    )
    monkeypatch.setattr("curatarr.services.client", lambda kind: radarr)
    shrink.request(media)
    assert shrink.status(media) == "pending"
    assert shrink.sweep() == 1
    assert radarr.calls == ["search", "delete 9", "grab Epic.1080p.WEB"]
    assert shrink.status(media) == "swapping"
    assert db.session.get(PurgeCandidate, candidate.id).state == "RESCUED"
    shrink.sweep()  # the file has arrived
    assert shrink.status(media) == "done"


def test_no_smaller_copy_changes_nothing(app, monkeypatch, client):
    media = _film("Epic", size=20 * GB)
    radarr = Radarr([_release("Epic.Remux", 19)])
    monkeypatch.setattr("curatarr.services.client", lambda kind: radarr)
    client.post(f"/titles/{media.id}/shrink/shrink")
    shrink.sweep()
    assert radarr.calls == ["search"]
    assert shrink.status(media) == "no_smaller"
    assert "No copy half the size or smaller" in client.get(f"/titles/{media.id}").text
    client.post(f"/titles/{media.id}/shrink/dismiss")
    assert shrink.status(media) is None


def test_upgradable_films_are_unmonitored_and_fall_back_if_nothing_arrives(
    app, monkeypatch
):
    media = _film("Epic", size=20 * GB)
    radarr = Radarr([_release("Epic.1080p", 6)], upgrades=True)
    monkeypatch.setattr("curatarr.services.client", lambda kind: radarr)
    shrink.request(media)
    shrink.sweep()
    assert radarr.calls[:2] == ["search", "monitored=False"]
    radarr.has_file = False
    entry = shrink.state(media)
    entry["swapped"]["at"] = (utcnow() - timedelta(days=3)).isoformat()
    shrink._set(media.id, swapped=entry["swapped"])
    db.session.commit()
    shrink.sweep()
    assert radarr.calls[-2:] == ["monitored=True", "search any"]


def test_cancel_before_the_lookup_finishes(app, monkeypatch):
    media = _film("Epic", size=20 * GB)
    radarr = Radarr([_release("Epic.1080p", 6)])
    monkeypatch.setattr("curatarr.services.client", lambda kind: radarr)
    shrink.request(media)
    shrink.cancel(media)
    shrink.sweep()
    assert radarr.calls == []


def test_small_films_and_shows_are_not_offered(app, client):
    small = _film("Short", size=2 * GB)
    assert not shrink.offered(small)
    big = _film("Long", size=20 * GB, tmdb=2)
    assert shrink.offered(big)
    assert "Shrink instead of delete" in client.get(f"/titles/{big.id}").text
    assert "Shrink instead of delete" not in client.get(f"/titles/{small.id}").text
