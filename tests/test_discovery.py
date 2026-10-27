import pytest

from curatarr import db
from curatarr.lifecycle import reconcile_candidates
from curatarr.models import MediaIdentity, MediaPart, PurgeCandidate
from curatarr.services import discover


@pytest.fixture(autouse=True)
def arr_configured(monkeypatch):
    """The fake clients stand in for a Sonarr and Radarr that are set up."""
    monkeypatch.setattr("curatarr.services.configured", lambda _kind: True)


class FakeJellyfin:
    def libraries(self):
        return [{"ItemId": "tv-demo", "Name": "Demo TV", "CollectionType": "tvshows"}]

    def items(self, _library_id, start, _limit):
        if start:
            return {"Items": [], "TotalRecordCount": 2}
        return {
            "Items": [
                {
                    "Id": "episode-1",
                    "Type": "Episode",
                    "SeriesId": "series-1",
                    "ParentIndexNumber": 1,
                    "IndexNumber": 1,
                },
                {
                    "Id": "series-1",
                    "Type": "Series",
                    "Name": "Sample Series",
                    "ProviderIds": {"Tvdb": "123"},
                },
            ],
            "TotalRecordCount": 2,
        }


class FakeSonarr:
    def series(self):
        return [{"id": 42, "tvdbId": 123}]

    def episodes(self, _series_id):
        return [
            {
                "id": 10,
                "seasonNumber": 1,
                "episodeNumber": 1,
                "episodeFileId": 51,
                "hasFile": True,
            },
            {"id": 11, "seasonNumber": 1, "episodeNumber": 2, "hasFile": False},
            {"id": 20, "seasonNumber": 2, "episodeNumber": 1, "hasFile": False},
        ]

    def episode_files(self, _series_id):
        return [{"id": 51, "size": 500, "dateAdded": "2024-03-02T10:00:00Z"}]


class FakeRadarr:
    def movies(self):
        return []


def test_discovery_keeps_missing_sonarr_episodes(app, monkeypatch):
    adapters = {
        "jellyfin": FakeJellyfin(),
        "sonarr": FakeSonarr(),
        "radarr": FakeRadarr(),
    }
    monkeypatch.setattr("curatarr.services.client", lambda kind: adapters[kind])
    with app.app_context():
        assert discover() == 1
        media = db.session.query(MediaIdentity).one()
        assert media.sonarr_id == 42
        parts = (
            db.session.query(MediaPart)
            .order_by(MediaPart.season_number, MediaPart.episode_number)
            .all()
        )
        assert len(parts) == 3
        assert parts[0].jellyfin_id == "episode-1"
        assert parts[0].size_bytes == 500
        # The acquisition time is Sonarr's file date, not when Curatarr looked.
        assert str(parts[0].acquired_at).startswith("2024-03-02")
        assert not parts[1].has_file


def test_renamed_series_and_changed_ids_keep_stable_provider_identity(app, monkeypatch):
    jellyfin = FakeJellyfin()
    sonarr = FakeSonarr()
    adapters = {"jellyfin": jellyfin, "sonarr": sonarr, "radarr": FakeRadarr()}
    monkeypatch.setattr("curatarr.services.client", lambda kind: adapters[kind])
    with app.app_context():
        discover()
        media = db.session.query(MediaIdentity).one()
        stable_id = media.id
        part_id = (
            db.session.query(MediaPart)
            .filter_by(season_number=1, episode_number=1)
            .one()
            .id
        )

        def rescanned_items(_library_id, start, _limit):
            if start:
                return {"Items": [], "TotalRecordCount": 2}
            return {
                "Items": [
                    {
                        "Id": "episode-new",
                        "Type": "Episode",
                        "SeriesId": "series-new",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                    },
                    {
                        "Id": "series-new",
                        "Type": "Series",
                        "Name": "Renamed Series",
                        "ProviderIds": {"Tvdb": "123"},
                    },
                ],
                "TotalRecordCount": 2,
            }

        jellyfin.items = rescanned_items
        sonarr.series = lambda: [{"id": 99, "tvdbId": 123}]
        assert discover() == 1
        assert db.session.query(MediaIdentity).count() == 1
        media = db.session.get(MediaIdentity, stable_id)
        assert (media.title, media.jellyfin_id, media.sonarr_id) == (
            "Renamed Series",
            "series-new",
            99,
        )
        assert db.session.get(MediaPart, part_id).jellyfin_id == "episode-new"


def test_external_movie_removal_rescues_pending_candidate(app, monkeypatch):
    has_file = True

    class MovieJellyfin:
        def libraries(self):
            return [{"ItemId": "movies", "Name": "Movies", "CollectionType": "movies"}]

        def items(self, _library_id, start, _limit):
            return {
                "Items": []
                if start
                else [
                    {
                        "Id": "film",
                        "Type": "Movie",
                        "Name": "Film",
                        "ProviderIds": {"Tmdb": "456"},
                    }
                ],
                "TotalRecordCount": 1,
            }

    class MovieRadarr:
        def movies(self):
            return [
                {
                    "id": 8,
                    "tmdbId": 456,
                    "hasFile": has_file,
                    "sizeOnDisk": 100 if has_file else 0,
                }
            ]

    adapters = {
        "jellyfin": MovieJellyfin(),
        "sonarr": FakeSonarr(),
        "radarr": MovieRadarr(),
    }
    monkeypatch.setattr("curatarr.services.client", lambda kind: adapters[kind])
    with app.app_context():
        discover()
        media = db.session.query(MediaIdentity).one()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        has_file = False
        discover()
        assert not db.session.query(MediaPart).one().has_file
        assert reconcile_candidates() == 1
        assert candidate.state == "RESCUED"


def test_discovery_without_radarr_matches_the_shows(app, monkeypatch):
    def client(kind):
        if kind == "radarr":
            raise AssertionError("Radarr isn't set up, so it isn't asked")
        return {"jellyfin": FakeJellyfin(), "sonarr": FakeSonarr()}[kind]

    monkeypatch.setattr("curatarr.services.client", client)
    monkeypatch.setattr("curatarr.services.configured", lambda kind: kind != "radarr")
    with app.app_context():
        assert discover() == 1
        assert db.session.query(MediaIdentity).one().sonarr_id == 42


class SonarrWithNewShows(FakeSonarr):
    """Sample Series (in Jellyfin) and shows Jellyfin doesn't list yet."""

    def __init__(self, extra):
        self.extra = extra

    def series(self):
        return [
            {"id": 42, "tvdbId": 123, "rootFolderPath": "/tv/"},
            *self.extra,
        ]

    def episodes(self, series_id):
        if series_id == 42:
            return super().episodes(series_id)
        return [
            {
                "id": series_id * 100 + n,
                "seasonNumber": s,
                "episodeNumber": n,
                "hasFile": False,
            }
            for s in (1, 2)
            for n in range(1, 7)
        ]

    def episode_files(self, series_id):
        return super().episode_files(series_id) if series_id == 42 else []


def _new_show(series_id, tvdb, title, *, added=None, monitored=True, files=0):
    from datetime import timedelta

    from curatarr.models import utcnow

    return {
        "id": series_id,
        "tvdbId": tvdb,
        "title": title,
        "monitored": monitored,
        "rootFolderPath": "/tv",
        "added": (added or utcnow() - timedelta(hours=1)).isoformat(),
        "statistics": {"episodeFileCount": files},
    }


def test_a_show_just_added_to_sonarr_is_taken_on_before_jellyfin_lists_it(
    app, monkeypatch
):
    """Jellyfin lists a show only once it has a file, and only the always-keep
    fill would fetch one: a new show must be known from Sonarr alone."""
    from datetime import timedelta

    from curatarr.models import utcnow
    from curatarr.services import _skip_reason

    sonarr = SonarrWithNewShows(
        [
            _new_show(77, 777, "Flipping Bangers"),
            _new_show(78, 778, "Long Ago", added=utcnow() - timedelta(days=400)),
            _new_show(79, 779, "Unmonitored", monitored=False),  # treated the same
            _new_show(80, 780, "Has Files Jellyfin Lacks", files=3),
        ]
    )
    jellyfin = FakeJellyfin()
    clients = {"jellyfin": jellyfin, "sonarr": sonarr, "radarr": FakeRadarr()}
    monkeypatch.setattr("curatarr.services.client", lambda kind: clients[kind])
    discover()
    titles = {m.title: m for m in db.session.query(MediaIdentity)}
    assert set(titles) == {"Sample Series", "Flipping Bangers", "Unmonitored"}
    new = titles["Flipping Bangers"]
    assert new.library_id == titles["Sample Series"].library_id  # same root folder
    assert new.sonarr_id == 77 and new.jellyfin_id is None and new.missing_since is None
    assert db.session.query(MediaPart).filter_by(media_identity_id=new.id).count() == 12
    assert _skip_reason(new) is None  # the always-keep fill can request it
    from curatarr.models import LifecycleAction
    from curatarr.services import reconcile_acquisition

    assert reconcile_acquisition() >= 1
    asked = db.session.query(LifecycleAction).filter_by(media_identity_id=new.id).all()
    assert asked and "always keeps" in asked[0].reason_text

    # Its first files arrive, then Jellyfin lists it: still the same title.
    sonarr.extra[0]["statistics"]["episodeFileCount"] = 2
    discover()
    assert db.session.get(MediaIdentity, new.id).missing_since is None

    class JellyfinListsIt(FakeJellyfin):
        def items(self, library_id, start, limit):
            page = super().items(library_id, start, limit)
            if not start:
                page["Items"] = page["Items"] + [
                    {
                        "Id": "series-2",
                        "Type": "Series",
                        "Name": "Flipping Bangers",
                        "ProviderIds": {"Tvdb": "777"},
                    }
                ]
            return page

    clients["jellyfin"] = JellyfinListsIt()
    discover()
    shows = db.session.query(MediaIdentity).filter_by(title="Flipping Bangers").all()
    assert (
        len(shows) == 1 and shows[0].id == new.id and shows[0].jellyfin_id == "series-2"
    )


def test_a_show_removed_from_sonarr_is_no_longer_sonarrs(app, monkeypatch):
    """Deleted in Sonarr, its empty folder kept it in Jellyfin; Curatarr still
    had its Sonarr id and asked Sonarr for its always-keep episodes."""
    from curatarr.services import _skip_reason

    sonarr = SonarrWithNewShows([])
    clients = {"jellyfin": FakeJellyfin(), "sonarr": sonarr, "radarr": FakeRadarr()}
    monkeypatch.setattr("curatarr.services.client", lambda kind: clients[kind])
    discover()
    media = db.session.query(MediaIdentity).filter_by(title="Sample Series").one()
    assert media.sonarr_id == 42
    sonarr.series = list  # removed from Sonarr; Jellyfin still lists it
    discover()
    db.session.refresh(media)
    assert media.sonarr_id is None and media.missing_since is None
    assert "matched to Sonarr" in _skip_reason(media)
