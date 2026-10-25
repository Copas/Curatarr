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
    with pytest.raises(ValueError, match="as a whole"):
        flags.set_flag(media, flags.LEAVE, season=1)


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


class Sonarr:
    """The show's six episodes, one file each."""

    def __init__(self):
        self.calls = []

    def queue(self):
        return {"records": []}

    def request(self, _method, _path):
        return {"tvdbId": 777}

    def episodes(self, _series_id):
        return [
            {"id": s * 100 + e, "hasFile": True, "episodeFileId": s * 1000 + e}
            for s, e in [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2)]
        ]

    def episode_files(self, _series_id):
        return [
            {"id": s * 1000 + e, "size": 100}
            for s, e in [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2)]
        ]

    def unmonitor_episode(self, episode_id):
        self.calls.append(("unmonitor", episode_id))
        return True

    def delete_episode_file(self, file_id):
        self.calls.append(("delete", file_id))


def _played(part, user="sam", when=None):
    from curatarr.models import EpisodeUserState as State

    state = (
        db.session.query(State)
        .filter_by(media_part_id=part.id, jellyfin_user_id=user)
        .first()
    ) or State(media_part_id=part.id, jellyfin_user_id=user)
    state.played = True
    state.last_played_at = when or utcnow() + timedelta(seconds=1)
    db.session.add(state)
    db.session.commit()


def test_a_season_leaves_episode_by_episode(app, jellyfin, monkeypatch):
    from curatarr.lifecycle import (
        WATCHED_EPISODES,
        execute_approved,
        reconcile_candidates,
        review_candidate,
    )

    media, parts = _show()
    db.session.add(
        LibraryPolicy(library_id=media.library_id, policy_json={"dry_run": False})
    )
    db.session.commit()
    row = flags.set_flag(media, flags.LEAVE, season=2, mode="each", user_id="sam")
    assert "Each episode of the season" in flags.describe(row)
    assert flags.sweep() == 0
    _played(parts[(2, 1)])
    assert flags.sweep() == 1
    candidate = _active_candidate(media.id)
    assert candidate.reason_code == WATCHED_EPISODES
    assert candidate.target_part_ids == [parts[(2, 1)].id]
    assert "S02E01" in candidate.reason_text and "not the show" in candidate.reason_text
    # Watching on doesn't rescue it: the next one joins it.
    _played(parts[(2, 2)], when=utcnow() + timedelta(seconds=2))
    _played(parts[(1, 4)], user="pat", when=utcnow() + timedelta(seconds=2))
    assert reconcile_candidates() == 0
    assert flags.sweep() == 1
    candidate = _active_candidate(media.id)
    assert set(candidate.target_part_ids) == {parts[(2, 1)].id, parts[(2, 2)].id}
    assert flags.sweep() == 0  # nothing new
    assert db.session.query(MediaFlag).count() == 1  # stays for later episodes
    # Removal deletes those two files only, unmonitored first.
    sonarr = Sonarr()
    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: jellyfin if kind == "jellyfin" else sonarr,
    )
    jellyfin.item = lambda _id: {"ProviderIds": {"Tvdb": "777"}}
    review_candidate(candidate.id, "delete")
    assert execute_approved(candidate.id) == "succeeded"
    assert sorted(c[1] for c in sonarr.calls if c[0] == "delete") == [2001, 2002]
    assert ("unmonitor", 201) in sonarr.calls
    assert not parts[(2, 1)].has_file and parts[(1, 4)].has_file
    # The season has no episodes left: the flag goes.
    flags.sweep()
    assert db.session.query(MediaFlag).count() == 0


def test_playing_a_leaving_episode_again_rescues_it(app, jellyfin):
    from curatarr.lifecycle import reconcile_candidates

    media, parts = _show()
    flags.set_flag(media, flags.LEAVE, part=parts[(1, 4)])
    _played(parts[(1, 4)])
    assert flags.sweep() == 1
    assert db.session.query(MediaFlag).count() == 0  # one episode: done
    candidate = _active_candidate(media.id)
    _played(parts[(1, 4)], user="pat", when=utcnow() + timedelta(minutes=5))
    assert reconcile_candidates() == 1
    assert candidate.state == "RESCUED"


def test_always_keep_episodes_never_leave(app, jellyfin):
    media, parts = _show()
    flags.set_flag(media, flags.LEAVE, part=parts[(1, 1)])  # first 3 are kept
    _played(parts[(1, 1)])
    assert flags.sweep() == 0
    assert _active_candidate(media.id) is None
    assert db.session.query(MediaFlag).count() == 0  # cleared, History says why


def test_a_whole_season_once_its_last_episode_is_watched(app, jellyfin):
    media, parts = _show()
    flags.set_flag(media, flags.LEAVE, season=1, mode="all")
    _played(parts[(1, 3)])
    assert flags.sweep() == 0
    _played(parts[(1, 4)])
    assert flags.sweep() == 1
    # Season 1's always-keep episodes stay: only S1E4 leaves.
    assert _active_candidate(media.id).target_part_ids == [parts[(1, 4)].id]


def test_removing_the_show_takes_over_from_its_episodes(app, jellyfin):
    from curatarr.lifecycle import request_removal

    media, parts = _show()
    flags.set_flag(media, flags.LEAVE, part=parts[(2, 2)])
    _played(parts[(2, 2)])
    flags.sweep()
    episodes = _active_candidate(media.id)
    whole = request_removal(media)
    assert episodes.state == "SUPERSEDED" and whole.target_part_ids is None


def test_a_shows_page_and_the_api_choose_what_leaves(app, client, jellyfin):
    media, parts = _show()
    page = client.get(f"/titles/{media.id}").text
    assert "Season 2: each episode as it's watched" in page
    client.post(
        f"/titles/{media.id}/flags",
        data={"flag": "leave_after_watched", "action": "set", "scope": "s2-each"},
    )
    row = db.session.query(MediaFlag).one()
    assert (row.season_number, row.mode) == (2, "each")
    set_setting("webhook_token", "secret")
    db.session.commit()
    view = client.post(
        "/api/v1/titles/by-jellyfin/ep-1-4",
        json={"action": "leave_after_watched"},
        headers={"X-Curatarr-Token": "secret"},
    ).get_json()
    assert {(f["season"], f["part"]) for f in view["flags"]} == {
        (2, None),
        (None, parts[(1, 4)].id),
    }
    page = client.get(f"/titles/{media.id}").text
    assert "Each episode of the season leaves once someone has watched it" in page
    assert "Clear season 2" in page and "Clear episode" in page


def test_a_protected_file_that_changes_size_is_reported(app, client, jellyfin):
    media = _film()
    flags.set_flag(media, flags.NEVER_COMPRESS)
    assert flags.check_protected() == []  # first seen
    part = media.parts[0]
    part.size_bytes = 60  # a transcoder rewrote it
    db.session.commit()
    changed = flags.check_protected()
    assert [(c["was"], c["now"]) for c in changed] == [(100, 60)]
    assert flags.check_protected() == []  # once
    page = client.get("/settings").text
    assert "1 protected file has changed size" in page
    client.post("/settings", data={"kind": "never_compress_dismiss"})
    assert flags.changed_files() == []


def test_the_plugins_menu_sets_leave_after_watched(app, client, jellyfin):
    from curatarr.jellyfin_delete import handle

    media, parts = _show()
    episode, show = "e4" * 16, "5e" * 16  # Jellyfin ids, as the plugin sends
    parts[(1, 4)].jellyfin_id, media.jellyfin_id = episode, show
    set_setting("jellyfin_user_names", {"sam": "Sam"})
    db.session.commit()
    _status, body = handle(episode, "Sam", "status")
    assert body["handled"] is False  # an episode: Delete stays Jellyfin's
    assert body["canLeaveAfterWatched"] and not body["leavesAfterWatched"]
    _status, body = handle(episode, "Sam", "leave_after_watched")
    assert body["result"] == "leave_after_watched"
    row = db.session.query(MediaFlag).one()
    assert (row.media_part_id, row.jellyfin_user_id) == (parts[(1, 4)].id, "sam")
    _status, body = handle(episode, "Sam", "status")
    assert body["leavesAfterWatched"] and not body["canLeaveAfterWatched"]
    _status, body = handle(show, "Sam", "status")
    assert body["handled"] and body["canLeaveAfterWatched"]
    _status, body = handle(episode, "Sam", "keep_after_watched")
    assert body["result"] == "cleared"
    assert db.session.query(MediaFlag).count() == 0


def test_the_poster_says_its_waiting_for_a_watch(app, jellyfin):
    from io import BytesIO

    from PIL import Image

    from curatarr import custom_posters

    picture = BytesIO()
    Image.new("RGB", (400, 600), "#223399").save(picture, format="JPEG")
    jellyfin.image = lambda _id, _kind="Primary": picture.getvalue()
    jellyfin.item_details = lambda ids: [
        {"Id": i, "ImageTags": {"Primary": "tag-1"}} for i in ids
    ]
    media = _film()
    set_setting("jellyfin_user_names", {"sam": "Sam", "pat": "Pat"})
    db.session.commit()
    flags.set_flag(media, flags.LEAVE, user_id="sam")
    entries = custom_posters.plugin_list()
    assert [(e["user"], e["id"]) for e in entries] == [("sam", f"v.sam.{media.id}")]
    shown = Image.open(
        BytesIO(custom_posters.plugin_picture(entries[0]["id"]))
    ).convert("RGB")
    amber = shown.getpixel((3, round(shown.height * 0.06)))
    assert amber[0] > 200 and 100 < amber[1] < 180 and amber[2] < 90  # top left
    # Anyone's watch: everyone sees it.
    flags.set_flag(media, flags.LEAVE)
    assert {e["user"] for e in custom_posters.plugin_list()} == {"sam", "pat"}
    flags.clear_flag(media, flags.LEAVE)
    assert custom_posters.plugin_list() == []
