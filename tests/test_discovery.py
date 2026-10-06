from curatarr import db
from curatarr.lifecycle import reconcile_candidates
from curatarr.models import MediaIdentity, MediaPart, PurgeCandidate
from curatarr.services import discover


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
        return [{"id": 42, "tvdbId": 123, "tags": [3, 9]}]

    def tags(self):
        return [{"id": 3, "label": "curatarr-pilot"}, {"id": 4, "label": "other"}]

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
        # Tag labels are stored; an id Sonarr no longer lists (9) is ignored.
        assert media.arr_tags == ["curatarr-pilot"]
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
