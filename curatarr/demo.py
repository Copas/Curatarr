"""Deterministic synthetic data and adapters for local exploration."""

import base64
import hashlib
from datetime import timedelta
from io import BytesIO

from flask import current_app
from PIL import Image

from . import db
from .integrations import IntegrationError
from .models import (
    AppSetting,
    Integration,
    Library,
    LibraryPolicy,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    TitleOverride,
    UserMediaState,
    utcnow,
)


def seed_demo():
    if db.session.query(Library).filter_by(jellyfin_library_id="demo-tv-a").first():
        return False
    now = utcnow()
    libraries = [
        Library(jellyfin_library_id="demo-tv-a", name="Demo TV A", media_type="tv"),
        Library(jellyfin_library_id="demo-tv-b", name="Demo TV B", media_type="tv"),
        Library(
            jellyfin_library_id="demo-movies", name="Demo Movies", media_type="movies"
        ),
    ]
    db.session.add_all(libraries)
    db.session.flush()
    db.session.add(
        LibraryPolicy(
            library_id=libraries[2].id,
            policy_json={
                "quota_enabled": True,
                "high_water_bytes": 80_000_000_000,
                "low_water_bytes": 70_000_000_000,
            },
        )
    )
    for index in range(1, 21):
        media = MediaIdentity(
            library_id=libraries[(index - 1) % 2].id,
            media_type="series",
            title=f"Demo Series {index:02d}",
            jellyfin_id=f"demo-series-{index}",
            sonarr_id=index,
            tvdb_id=10000 + index,
            added_at=now - timedelta(days=180 + index),
        )
        db.session.add(media)
        db.session.flush()
        for season in (1, 2, 3):
            for episode in range(1, 5):
                stored = season == 1 and episode <= (2 if index == 1 else 4)
                db.session.add(
                    MediaPart(
                        media_identity_id=media.id,
                        kind="episode",
                        season_number=season,
                        episode_number=episode,
                        jellyfin_id=f"demo-episode-{index}-{season}-{episode}"
                        if stored
                        else None,
                        sonarr_episode_id=index * 100 + season * 10 + episode,
                        arr_file_id=index * 100 + season * 10 + episode
                        if stored
                        else None,
                        has_file=stored,
                        size_bytes=400_000_000 if stored else 0,
                        air_date=now + timedelta(days=7)
                        if index == 2 and season == 3
                        else now - timedelta(days=60),
                    )
                )
        if index in (3, 6):
            db.session.add(
                UserMediaState(
                    media_identity_id=media.id,
                    jellyfin_user_id="demo-viewer-a",
                    favorite=index == 3,
                    last_played_at=now - timedelta(days=10),
                )
            )
        if index == 4:
            db.session.add(
                TitleOverride(
                    media_identity_id=media.id, override_json={"never_purge": True}
                )
            )
        if index == 5:
            db.session.add(
                PurgeCandidate(
                    media_identity_id=media.id,
                    state="LEAVING_SOON",
                    reason_code="inactivity",
                    reason_text="Demo: no playback for 120 days.",
                    reclaimable_bytes=400_000_000,
                    scheduled_delete_at=now + timedelta(days=14),
                )
            )
    for index in range(1, 51):
        media = MediaIdentity(
            library_id=libraries[2].id,
            media_type="movie",
            title=f"Demo Movie {index:02d}",
            jellyfin_id=f"demo-movie-{index}",
            radarr_id=index,
            tmdb_id=20000 + index,
            added_at=now - timedelta(days=200 if index in (7, 8) else 40 + index * 4),
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=2_000_000_000,
            )
        )
        if index % 5 == 0:
            db.session.add(
                UserMediaState(
                    media_identity_id=media.id,
                    jellyfin_user_id="demo-viewer-b",
                    favorite=index == 10,
                    last_played_at=now - timedelta(days=90),
                )
            )
        if index in (7, 8):
            state = "REVIEW" if index == 7 else "ELIGIBLE"
            candidate = PurgeCandidate(
                media_identity_id=media.id,
                state=state,
                reason_code="inactivity",
                reason_text="No playback observed for more than 90 days.",
                reclaimable_bytes=2_000_000_000,
                score_detail_json={"acquired_days": 40, "unwatched_bonus": 30},
            )
            db.session.add(candidate)
    for library in libraries:
        library.last_size_bytes = sum(
            part.size_bytes
            for media in db.session.query(MediaIdentity).filter_by(
                library_id=library.id
            )
            for part in media.parts
            if part.has_file
        )
    for kind in ("jellyfin", "sonarr", "radarr"):
        db.session.add(
            Integration(
                kind=kind,
                base_url="http://demo.invalid",
                secret_ref="demo",
                health_state="healthy",
                detected_version="demo",
            )
        )
    db.session.commit()
    return True


class DemoClient:
    def __init__(self, kind):
        self.kind = kind

    def _check(self):
        row = db.session.query(AppSetting).filter_by(key="demo_outage").first()
        if row and row.value_json == self.kind:
            raise IntegrationError(f"Demo {self.kind} outage")

    def health(self):
        self._check()
        return {"version": "demo"}

    def item(self, item_id):
        self._check()
        media = db.session.query(MediaIdentity).filter_by(jellyfin_id=item_id).first()
        if not media:
            raise IntegrationError("Demo item missing")
        return {
            "Id": item_id,
            "ImageTags": {"Primary": hashlib.sha256(self.image(item_id)).hexdigest()},
            "ProviderIds": {
                "Tvdb": str(media.tvdb_id) if media.tvdb_id else None,
                "Tmdb": str(media.tmdb_id) if media.tmdb_id else None,
            },
        }

    def queue(self):
        self._check()
        key = "seriesId" if self.kind == "sonarr" else "movieId"
        return {"records": [{key: 2}]}

    def commands(self):
        self._check()
        return []

    def movie(self, movie_id):
        return self.request("GET", f"/api/v3/movie/{movie_id}")

    def _episode_parts(self, series_id):
        media = db.session.query(MediaIdentity).filter_by(sonarr_id=series_id).first()
        if not media:
            raise IntegrationError("Demo series missing")
        return [part for part in media.parts if part.kind == "episode"]

    def episodes(self, series_id):
        self._check()
        return [
            {
                "id": part.sonarr_episode_id,
                "seasonNumber": part.season_number,
                "episodeNumber": part.episode_number,
                "hasFile": part.has_file,
                "episodeFileId": part.arr_file_id if part.has_file else 0,
            }
            for part in self._episode_parts(series_id)
        ]

    def episode_files(self, series_id):
        self._check()
        return [
            {"id": part.arr_file_id, "size": part.size_bytes}
            for part in self._episode_parts(series_id)
            if part.has_file and part.arr_file_id
        ]

    def diskspace(self):
        self._check()
        return [
            {
                "path": "/demo/media",
                "totalSpace": 100_000_000_000,
                "freeSpace": 10_000_000_000,
            }
        ]

    def request(self, method, path, **_kwargs):
        self._check()
        external_id = int(path.rstrip("/").split("/")[-1])
        if self.kind == "sonarr":
            return {"id": external_id, "tvdbId": 10000 + external_id}
        return {"id": external_id, "tmdbId": 20000 + external_id}

    def monitor_episode(self, _episode_id):
        self._check()

    def season_search(self, _series_id, _season_number):
        self._check()

    def delete_movie(self, _movie_id):
        self._check()

    def delete_episode_file(self, _file_id):
        self._check()

    def image(self, _item_id):
        row = (
            db.session.query(AppSetting).filter_by(key=f"demo_image:{_item_id}").first()
        )
        if row:
            return base64.b64decode(row.value_json)
        output = BytesIO()
        Image.new("RGB", (300, 450), "#456070").save(output, format="JPEG")
        return output.getvalue()

    def put_image(self, _item_id, _image_bytes):
        self._check()
        row = (
            db.session.query(AppSetting).filter_by(key=f"demo_image:{_item_id}").first()
        )
        if not row:
            row = AppSetting(key=f"demo_image:{_item_id}", value_json="")
            db.session.add(row)
        row.value_json = base64.b64encode(_image_bytes).decode("ascii")
        db.session.commit()

    def mark_played(self, _user_id, _item_id):
        self._check()


def demo_client(kind):
    if not current_app.config["DEMO_MODE"]:
        raise RuntimeError("Demo adapter requested outside demo mode")
    return DemoClient(kind)
