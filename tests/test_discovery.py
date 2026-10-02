from curatarr import db
from curatarr.models import MediaIdentity, MediaPart
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
        return [{"id": 51, "size": 500}]


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
        assert not parts[1].has_file
