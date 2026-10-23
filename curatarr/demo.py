"""Deterministic synthetic data and adapters for local exploration."""

import base64
import hashlib
import secrets
from datetime import timedelta
from io import BytesIO

from flask import current_app
from PIL import Image
from sqlalchemy import func

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
    # The demo shows the review flow for unwatched titles, so its libraries
    # opt into inactivity cleanup (off by default for real libraries).
    for library in libraries[:2]:
        db.session.add(
            LibraryPolicy(
                library_id=library.id, policy_json={"inactivity_cleanup": True}
            )
        )
    db.session.add(
        LibraryPolicy(
            library_id=libraries[2].id,
            policy_json={
                "inactivity_cleanup": True,
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
    db.session.add(
        AppSetting(
            key="jellyfin_user_names",
            value_json={"demo-viewer-a": "Avery", "demo-viewer-b": "Blake"},
        )
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


# Demo accounts: "demo-admin" is a Jellyfin administrator and "demo-viewer"
# is not. Both use the password "demo".
DEMO_USERS = {"demo-admin": True, "demo-viewer": False}


SIMULATIONS = {
    "complete_episode",
    "start_playback",
    "favorite",
    "unfavorite",
    "advance_30_days",
    "add_movie",
    "increase_movie_size",
    "outage_sonarr",
    "outage_radarr",
    "outage_jellyfin",
    "restore_integrations",
}


def simulate(choice):
    """Apply one Overview demo control; raises ValueError for unknown choices."""
    from .services import ingest_event, process_pending_events, set_setting

    if choice not in SIMULATIONS:
        raise ValueError("Unknown demo simulation")
    seed_demo()
    if choice in {"complete_episode", "start_playback", "favorite", "unfavorite"}:
        is_episode = choice in {"complete_episode", "start_playback"}
        ingest_event(
            {
                "event_id": secrets.token_hex(16),
                "event_type": {
                    "complete_episode": "item_played",
                    "start_playback": "playback_started",
                }.get(choice, "favorite_changed"),
                "item_external_id": "demo-episode-1-1-1"
                if is_episode
                else "demo-series-3",
                "series_external_id": "demo-series-1"
                if is_episode
                else "demo-series-3",
                "season_number": 1,
                "episode_number": 1,
                "user_external_id": "demo-viewer-a",
                "played": choice == "complete_episode",
                "favorite": choice == "favorite" if not is_episode else None,
            }
        )
        process_pending_events()
    elif choice == "advance_30_days":
        for media in db.session.query(MediaIdentity).all():
            if media.added_at:
                media.added_at -= timedelta(days=30)
        for state in db.session.query(UserMediaState).all():
            if state.last_played_at:
                state.last_played_at -= timedelta(days=30)
        db.session.commit()
    elif choice == "increase_movie_size":
        media = (
            db.session.query(MediaIdentity)
            .filter_by(jellyfin_id="demo-movie-1")
            .first()
        )
        if media:
            media.parts[0].size_bytes += 1_000_000_000
            db.session.commit()
    elif choice == "add_movie":
        library = (
            db.session.query(Library).filter_by(jellyfin_library_id="demo-movies").one()
        )
        next_id = (
            db.session.query(func.max(MediaIdentity.radarr_id)).scalar() or 50
        ) + 1
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Demo Movie {next_id:02d}",
            jellyfin_id=f"demo-movie-{next_id}",
            radarr_id=next_id,
            tmdb_id=20000 + next_id,
            added_at=utcnow(),
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=2_000_000_000,
                acquired_at=utcnow(),
            )
        )
        library.last_size_bytes = (library.last_size_bytes or 0) + 2_000_000_000
        db.session.commit()
    else:
        set_setting(
            "demo_outage",
            None if choice == "restore_integrations" else choice.split("_")[1],
        )
        db.session.commit()


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
        last = path.rstrip("/").split("/")[-1]
        if not last.isdigit():
            # Anything else, such as Intro Skipper's segments: the demo server
            # has no plugins, so answer as a server without them would.
            raise IntegrationError(f"Demo {self.kind} has no {path}", 404)
        external_id = int(last)
        if self.kind == "sonarr":
            return {"id": external_id, "tvdbId": 10000 + external_id}
        return {"id": external_id, "tmdbId": 20000 + external_id}

    # --- Radarr collections and recommendations (Library → Collections) ---

    def _demo_movies(self):
        return (
            db.session.query(MediaIdentity)
            .filter(MediaIdentity.radarr_id.isnot(None))
            .order_by(MediaIdentity.title)
            .all()
        )

    def _added(self):
        row = db.session.query(AppSetting).filter_by(key="demo_added_films").first()
        return set(row.value_json or []) if row else set()

    def _film(self, tmdb_id, title, year, genres, status="released", existing=False):
        return {
            "tmdbId": tmdb_id,
            "title": title,
            "year": year,
            "status": status,
            "genres": genres,
            "runtime": 95,
            "ratings": {"tmdb": {"value": 6.0 + (tmdb_id % 30) / 10}},
            "overview": f"A synthetic demo film ({title}).",
            "isExisting": existing or tmdb_id in self._added(),
            "isExcluded": False,
        }

    def movies(self):
        self._check()
        return [
            {
                "id": media.radarr_id,
                "tmdbId": media.tmdb_id,
                "title": media.title,
                "genres": ["Comedy", "Drama"] if n % 2 else ["Horror", "Thriller"],
                "qualityProfileId": 1,
                "rootFolderPath": "/demo/movies",
                "collection": {"tmdbId": 900} if n < 2 else None,
            }
            for n, media in enumerate(self._demo_movies())
        ]

    def collections(self):
        self._check()
        owned = self._demo_movies()[:2]
        films = [
            self._film(
                media.tmdb_id or 30000 + n,
                media.title,
                2001 + n,
                ["Horror"],
                existing=True,
            )
            for n, media in enumerate(owned)
        ] + [
            self._film(30101, "Demo Saga III", 2010, ["Horror"]),
            self._film(30102, "Demo Saga IV", 2014, ["Horror"]),
            self._film(30103, "Demo Saga V", 2099, ["Horror"], status="announced"),
        ]
        return [
            {
                "id": 1,
                "tmdbId": 900,
                "title": "Demo Saga Collection",
                "monitored": False,
                "movies": films,
            }
        ]

    def collection(self, collection_id):
        return next(c for c in self.collections() if c["id"] == collection_id)

    def update_collection(self, collection):
        self._check()
        return collection

    def recommendations(self):
        self._check()
        return [
            self._film(30200 + n, f"Demo Pick {n + 1}", 1990 + n, genres)
            for n, genres in enumerate(
                [
                    ["Horror"],
                    ["Comedy"],
                    ["Crime", "Thriller"],
                    ["Drama"],
                    ["Horror", "Comedy"],
                ]
            )
        ]

    def lookup_tmdb(self, tmdb_id):
        self._check()
        return {"tmdbId": tmdb_id, "title": f"Demo film {tmdb_id}", "year": 2000}

    def add_movie(self, movie):
        self._check()
        row = db.session.query(AppSetting).filter_by(key="demo_added_films").first()
        if not row:
            row = AppSetting(key="demo_added_films", value_json=[])
            db.session.add(row)
        row.value_json = sorted(set(row.value_json or []) | {movie["tmdbId"]})
        db.session.commit()
        return movie | {"id": 50000 + movie["tmdbId"]}

    def monitor_episode(self, _episode_id):
        self._check()

    def season_search(self, _series_id, _season_number):
        self._check()

    def delete_movie(self, _movie_id, *, exclude=False):
        self._check()

    def list_movies(self):
        self._check()
        return []

    def unmonitor_episode(self, _episode_id):
        self._check()
        return True

    def delete_episode_file(self, _file_id):
        self._check()

    def image(self, _item_id, _kind="Primary"):
        row = (
            db.session.query(AppSetting).filter_by(key=f"demo_image:{_item_id}").first()
        )
        if row:
            return base64.b64decode(row.value_json)
        output = BytesIO()
        Image.new("RGB", (300, 450), "#456070").save(output, format="JPEG")
        return output.getvalue()

    def image_list(self, _item_id):
        return []

    def put_image(self, _item_id, _image_bytes, _kind="Primary"):
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

    def plugins(self):
        self._check()
        return []

    def repositories(self):
        raise IntegrationError("The demo has no Jellyfin to install plugins in")

    def sessions(self):
        self._check()
        return []

    def refresh_item(self, _item_id):
        self._check()

    def _demo_user(self, name):
        return {
            "Id": f"user-{name}",
            "Name": name,
            "Policy": {"IsAdministrator": DEMO_USERS[name], "IsDisabled": False},
        }

    def authenticate(self, username, password, _device_id):
        self._check()
        if username not in DEMO_USERS or password != "demo":
            raise IntegrationError("Demo sign-in failed", 401)
        return {"User": self._demo_user(username), "AccessToken": "demo-token"}

    def end_session(self, _token, _device_id):
        self._check()

    def user(self, user_id):
        self._check()
        name = user_id.removeprefix("user-")
        if name not in DEMO_USERS:
            raise IntegrationError("Demo user missing", 404)
        return self._demo_user(name)

    def users(self):
        self._check()
        return [self._demo_user(name) for name in DEMO_USERS]


def demo_client(kind):
    if not current_app.config["DEMO_MODE"]:
        raise RuntimeError("Demo adapter requested outside demo mode")
    return DemoClient(kind)
