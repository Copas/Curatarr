"""Flags: Leave after watched and Never compress, on the title page and
through the small JSON API (issue #1)."""

from datetime import timedelta

import pytest

from curatarr import db, flags
from curatarr.lifecycle import MANUAL_RESET, REQUESTED_REMOVAL, _active_candidate
from curatarr.models import (
    EpisodeUserState,
    Library,
    LibraryPolicy,
    MediaFlag,
    MediaIdentity,
    MediaPart,
    TitleOverride,
    UserMediaState,
    utcnow,
)
from curatarr.services import set_setting


def _film():
    library = Library(jellyfin_library_id="films", name="Movies", media_type="movies")
    db.session.add(library)
    db.session.flush()
    db.session.add(LibraryPolicy(library_id=library.id, policy_json={"dry_run": False}))
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title="One Last Time",
        jellyfin_id="film-1",
        radarr_id=42,
        tmdb_id=321,
        added_at=utcnow() - timedelta(days=300),
    )
    db.session.add(media)
    db.session.flush()
    db.session.add(
        MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            size_bytes=100,
            arr_file_id=7,
            file_path="/mnt/media/films/One Last Time (2001)/film.mkv",
        )
    )
    db.session.add(
        UserMediaState(
            media_identity_id=media.id,
            jellyfin_user_id="sam",
            last_played_at=utcnow() - timedelta(days=200),
        )
    )
    db.session.commit()
    return media


def _show():
    library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title="A Show",
        jellyfin_id="show-1",
        sonarr_id=77,
        tvdb_id=777,
        added_at=utcnow() - timedelta(days=300),
    )
    db.session.add(media)
    db.session.flush()
    parts = {}
    for season, episode in [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2)]:
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=season,
            episode_number=episode,
            sonarr_episode_id=season * 100 + episode,
            arr_file_id=season * 1000 + episode,
            has_file=True,
            size_bytes=100,
            jellyfin_id=f"ep-{season}-{episode}",
            file_path=f"/mnt/media/tv/A Show/Season {season}/e{episode}.mkv",
        )
        db.session.add(part)
        parts[(season, episode)] = part
    db.session.commit()
    return media, parts


class Jellyfin:
    """Each account's play state for the film; seasons by id."""

    def __init__(self):
        self.played = {}  # user → when Jellyfin last marked the film played

    def users(self):
        return []

    def plugins(self):
        return []

    def packages(self):
        return []

    def sessions(self):
        return []

    def user_data(self, user_id, _item_id):
        when = self.played.get(user_id)
        return {"Played": when is not None, "LastPlayedDate": when}

    def item(self, item_id):
        if item_id == "season-2":
            return {"Type": "Season", "SeriesId": "show-1", "IndexNumber": 2}
        return {"Type": "Movie", "ProviderIds": {"Tmdb": "321"}}


@pytest.fixture
def jellyfin(monkeypatch):
    fake = Jellyfin()
    monkeypatch.setattr("curatarr.services.client", lambda _kind: fake)
    return fake


def test_a_film_leaves_once_watched_after_the_flag(app, jellyfin):
    media = _film()
    set_setting("jellyfin_user_names", {"sam": "Sam", "pat": "Pat"})
    row = flags.set_flag(media, flags.LEAVE, user_id="sam")
    assert flags.describe(row) == "Leaving Soon once Sam has watched it"
    # Watched before the flag was set: that doesn't count.
    jellyfin.played["sam"] = (utcnow() - timedelta(days=200)).isoformat()
    assert flags.sweep() == 0 and _active_candidate(media.id) is None
    # Someone else watching it doesn't count either: Sam's watch does.
    jellyfin.played["pat"] = utcnow().isoformat()
    db.session.add(UserMediaState(media_identity_id=media.id, jellyfin_user_id="pat"))
    db.session.commit()
    assert flags.sweep() == 0
    jellyfin.played["sam"] = (utcnow() + timedelta(seconds=1)).isoformat()
    assert flags.sweep() == 1
    candidate = _active_candidate(media.id)
    assert candidate.state == "LEAVING_SOON"
    assert candidate.reason_code == REQUESTED_REMOVAL
    assert "Leave after watched" in candidate.reason_text
    assert db.session.query(MediaFlag).count() == 0  # done with


def test_anyones_watch_can_count(app, jellyfin):
    media = _film()
    flags.set_flag(media, flags.LEAVE)
    jellyfin.played["sam"] = (utcnow() + timedelta(seconds=1)).isoformat()
    assert flags.sweep() == 1


def test_a_show_leaves_when_its_last_episode_is_watched(app, jellyfin):
    media, parts = _show()
    flags.set_flag(media, flags.LEAVE, user_id="sam")
    later = utcnow() + timedelta(seconds=1)
    # An earlier episode, then the last one by someone else: still waiting.
    db.session.add_all(
        [
            EpisodeUserState(
                media_part_id=parts[(1, 4)].id,
                jellyfin_user_id="sam",
                played=True,
                last_played_at=later,
            ),
            EpisodeUserState(
                media_part_id=parts[(2, 2)].id,
                jellyfin_user_id="pat",
                played=True,
                last_played_at=later,
            ),
        ]
    )
    db.session.commit()
    assert flags.sweep() == 0
    db.session.add(
        EpisodeUserState(
            media_part_id=parts[(2, 2)].id,
            jellyfin_user_id="sam",
            played=True,
            last_played_at=later,
        )
    )
    db.session.commit()
    assert flags.sweep() == 1
    assert _active_candidate(media.id).reason_code == MANUAL_RESET  # trimmed


def test_leave_after_watched_respects_never_purge(app, jellyfin):
    media = _film()
    db.session.add(
        TitleOverride(media_identity_id=media.id, override_json={"never_purge": True})
    )
    db.session.commit()
    with pytest.raises(ValueError, match="Never Purge"):
        flags.set_flag(media, flags.LEAVE)
    with pytest.raises(ValueError, match="whole film or show"):
        flags.set_flag(_show()[0], flags.LEAVE, season=1)


def test_never_compress_lists_the_files_to_leave_alone(app, jellyfin, tmp_path):
    film = _film()
    show, parts = _show()
    assert flags.protected_paths() == []
    flags.set_flag(film, flags.NEVER_COMPRESS)
    flags.set_flag(show, flags.NEVER_COMPRESS, season=2)
    assert flags.protected_paths() == sorted(
        [
            "/mnt/media/films/One Last Time (2001)/film.mkv",
            "/mnt/media/tv/A Show/Season 2/e1.mkv",
            "/mnt/media/tv/A Show/Season 2/e2.mkv",
        ]
    )
    flags.clear_flag(show, flags.NEVER_COMPRESS, season=2)
    flags.set_flag(show, flags.NEVER_COMPRESS, part=parts[(1, 1)])
    assert "/mnt/media/tv/A Show/Season 1/e1.mkv" in flags.protected_paths()
    assert len(flags.protected_paths()) == 2
    # Favourites too, when asked; and the list written to a file.
    db.session.add(
        EpisodeUserState(
            media_part_id=parts[(1, 3)].id, jellyfin_user_id="sam", favorite=True
        )
    )
    db.session.commit()
    listing = tmp_path / "skip.txt"
    flags.save_settings(True, str(listing))
    assert "/mnt/media/tv/A Show/Season 1/e3.mkv\n" in listing.read_text()
    assert len(listing.read_text().splitlines()) == 3
    with pytest.raises(ValueError, match="full path"):
        flags.save_settings(True, "skip.txt")


def test_the_title_page_sets_and_clears_them(app, client, jellyfin):
    media = _film()
    page = client.get(f"/titles/{media.id}").text
    assert "Leave after watched" in page and "Never compress" in page
    client.post(
        f"/titles/{media.id}/flags",
        data={"flag": "leave_after_watched", "action": "set", "user": ""},
    )
    assert (
        "Leaving Soon once someone has watched it"
        in client.get(f"/titles/{media.id}").text
    )
    client.post(
        f"/titles/{media.id}/flags",
        data={"flag": "leave_after_watched", "action": "clear"},
    )
    assert db.session.query(MediaFlag).count() == 0
    client.post(
        f"/titles/{media.id}/flags", data={"flag": "never_compress", "action": "set"}
    )
    assert "/api/v1/protected-paths" in client.get("/settings").text


def test_the_json_api(app, client, jellyfin):
    _film()
    _show()
    set_setting("webhook_token", "secret")
    db.session.commit()
    token = {"X-Curatarr-Token": "secret"}
    view = client.get("/api/v1/titles/by-jellyfin/film-1").get_json()
    assert view["title"] == "One Last Time" and view["state"] is None
    assert client.get("/api/v1/titles/by-jellyfin/nothing").status_code == 404
    # Changing anything takes the webhook token.
    assert (
        client.post(
            "/api/v1/titles/by-jellyfin/film-1", json={"action": "never_compress"}
        ).status_code
        == 403
    )
    view = client.post(
        "/api/v1/titles/by-jellyfin/film-1",
        json={"action": "leave_after_watched", "user": "sam"},
        headers=token,
    ).get_json()
    assert view["flags"][0]["flag"] == "leave_after_watched"
    assert view["flags"][0]["user"] == "sam"
    # A season, by its Jellyfin id; an episode by its own.
    view = client.post(
        "/api/v1/titles/by-jellyfin/season-2",
        json={"action": "never_compress"},
        headers=token,
    ).get_json()
    assert view["level"] == "season" and view["season"] == 2
    view = client.post(
        "/api/v1/titles/by-jellyfin/ep-1-1",
        json={"action": "never_compress"},
        headers=token,
    ).get_json()
    assert view["level"] == "episode" and view["episode"] == 1
    paths = client.get("/api/v1/protected-paths?kind=compress").text.splitlines()
    assert len(paths) == 3
    # Review choices need something up for removal.
    response = client.post(
        "/api/v1/titles/by-jellyfin/show-1", json={"action": "keep"}, headers=token
    )
    assert response.status_code == 409
    view = client.post(
        "/api/v1/titles/by-jellyfin/ep-2-2",
        json={"action": "never_purge"},
        headers=token,
    ).get_json()
    assert view["never_purge"] is True
    response = client.post(
        "/api/v1/titles/by-jellyfin/show-1",
        json={"action": "leave_after_watched"},
        headers=token,
    )
    assert response.status_code == 409 and "Never Purge" in response.get_json()["error"]
    view = client.post(
        "/api/v1/titles/by-jellyfin/film-1",
        json={"action": "clear", "flag": "leave_after_watched"},
        headers=token,
    ).get_json()
    assert view["flags"] == []
