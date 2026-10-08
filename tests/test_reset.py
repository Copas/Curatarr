"""Title page "Reset to minimum": trim a show to its always-keep episodes as if
nobody had watched it, through the normal approved-deletion path."""

from datetime import timedelta

from curatarr import db
from curatarr.lifecycle import reset_series
from curatarr.models import (
    EpisodeUserState,
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    TitleOverride,
    utcnow,
)
from curatarr.services import reconcile_acquisition


def _show(*, dry_run=False, added_days_ago=200):
    """Season 1 (4 episodes) and Season 2 (2 episodes), all downloaded.
    Always-keep is the first 3 episodes of Season 1 (the default)."""
    library = Library(jellyfin_library_id="tv-reset", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    db.session.add(
        LibraryPolicy(library_id=library.id, policy_json={"dry_run": dry_run})
    )
    media = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title="Reset Show",
        jellyfin_id="reset-show",
        sonarr_id=77,
        tvdb_id=777,
        arr_monitored=True,
        series_type="standard",
        added_at=utcnow() - timedelta(days=added_days_ago),
    )
    db.session.add(media)
    db.session.flush()
    parts = {}
    aired = utcnow() - timedelta(days=400)
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
            air_date=aired,
        )
        db.session.add(part)
        parts[(season, episode)] = part
    db.session.commit()
    return media, parts


class FakeSonarr:
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

    def unmonitor_seasons(self, series_id):
        self.calls.append(("unmonitor_seasons", series_id))
        return [1, 2]

    def set_episodes_monitored(self, ids, monitored):
        self.calls.append(("monitored", tuple(sorted(ids)), monitored))


class FakeJellyfin:
    def item(self, _item_id):
        return {"ProviderIds": {"Tvdb": "777"}}


def _clients(monkeypatch):
    sonarr = FakeSonarr()
    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else sonarr,
    )
    return sonarr


def _watched(part, days_ago):
    db.session.add(
        EpisodeUserState(
            media_part_id=part.id,
            jellyfin_user_id="viewer",
            played=True,
            last_played_at=utcnow() - timedelta(days=days_ago),
        )
    )
    db.session.commit()


def test_reset_trims_to_the_minimum_and_stops_further_downloads(app, monkeypatch):
    with app.app_context():
        media, parts = _show()
        sonarr = _clients(monkeypatch)
        candidate, result = reset_series(media)
        assert result == "succeeded"
        deleted = sorted(c[1] for c in sonarr.calls if c[0] == "delete")
        assert deleted == [1004, 2001, 2002]  # S1E4 and Season 2; S1E1-3 stay
        assert ("unmonitor_seasons", 77) in sonarr.calls
        assert ("monitored", (101, 102, 103), True) in sonarr.calls
        assert ("monitored", (104, 201, 202), False) in sonarr.calls
        assert [p.has_file for p in parts.values()] == [
            True,
            True,
            True,
            False,
            False,
            False,
        ]
        assert media.viewing_reset_at is not None
        assert candidate.state == "COMPLETED"


def test_reset_ignores_grace_period_and_recent_viewing(app, monkeypatch):
    with app.app_context():
        media, parts = _show(added_days_ago=2)  # inside the default grace period
        _watched(parts[(2, 1)], days_ago=1)  # watched yesterday
        _clients(monkeypatch)
        _candidate, result = reset_series(media)
        assert result == "succeeded"


def test_reset_respects_dry_run(app, monkeypatch):
    with app.app_context():
        media, parts = _show(dry_run=True)
        sonarr = _clients(monkeypatch)
        _candidate, result = reset_series(media)
        assert result == "dry_run"
        assert sonarr.calls == []
        assert all(p.has_file for p in parts.values())
        assert media.viewing_reset_at is None


def test_never_purge_blocks_a_reset(app, monkeypatch):
    with app.app_context():
        media, _parts = _show()
        db.session.add(
            TitleOverride(
                media_identity_id=media.id, override_json={"never_purge": True}
            )
        )
        db.session.commit()
        sonarr = _clients(monkeypatch)
        candidate, result = reset_series(media)
        assert result == "blocked"
        assert candidate.state == "BLOCKED"
        assert not [c for c in sonarr.calls if c[0] == "delete"]


def test_viewing_before_a_reset_no_longer_fetches_seasons(app, monkeypatch):
    with app.app_context():
        media, parts = _show()
        _watched(parts[(1, 4)], days_ago=5)
        _clients(monkeypatch)
        reset_series(media)
        db.session.query(LifecycleAction).filter_by(
            action_type="sonarr_season_search"
        ).delete()
        db.session.commit()
        assert reconcile_acquisition() == 0
        # Watching again after the reset counts as usual.
        state = db.session.query(EpisodeUserState).one()
        state.last_played_at = utcnow()
        db.session.commit()
        assert reconcile_acquisition() >= 1


def test_title_page_offers_the_reset(app, client):
    with app.app_context():
        media, _parts = _show(dry_run=True)
        page = client.get(f"/titles/{media.id}").text
        assert "Reset to minimum" in page
        assert f"/titles/{media.id}/reset" in page
        confirm = client.get(f"/titles/{media.id}/reset").text
        assert "Reset Reset Show to minimum?" in confirm
        assert "3 episode files" in confirm
        assert "dry run" in confirm
        assert "Cancel" in confirm


def test_reset_works_on_an_unmonitored_show_missing_an_always_keep_episode(
    app, monkeypatch
):
    with app.app_context():
        media, parts = _show()
        media.arr_monitored = False  # e.g. the nightly "unmonitor ended shows" job
        parts[(1, 2)].has_file = False  # an always-keep episode is missing
        db.session.commit()
        sonarr = _clients(monkeypatch)
        sonarr.episode_files = lambda _series_id: [
            {"id": s * 1000 + e, "size": 100}
            for s, e in [(1, 1), (1, 3), (1, 4), (2, 1), (2, 2)]
        ]
        sonarr.episodes = lambda _series_id: [
            {
                "id": s * 100 + e,
                "hasFile": (s, e) != (1, 2),
                "episodeFileId": s * 1000 + e if (s, e) != (1, 2) else 0,
            }
            for s, e in [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2)]
        ]
        _candidate, result = reset_series(media)
        assert result == "succeeded"
        assert sorted(c[1] for c in sonarr.calls if c[0] == "delete") == [
            1004,
            2001,
            2002,
        ]


def test_a_reset_unmonitored_show_is_refilled_and_remonitored_when_watched(
    app, monkeypatch
):
    from curatarr.lifecycle import execute_action

    with app.app_context():
        media, parts = _show()
        media.arr_monitored = False
        db.session.commit()
        _clients(monkeypatch)
        reset_series(media)
        for key in [(1, 4), (2, 1), (2, 2)]:
            assert not parts[key].has_file
        db.session.query(LifecycleAction).filter_by(
            action_type="sonarr_season_search"
        ).delete()
        db.session.commit()
        _watched(parts[(1, 3)], days_ago=0)  # watched after the reset
        assert reconcile_acquisition() >= 1
        action = (
            db.session.query(LifecycleAction)
            .filter_by(action_type="sonarr_season_search")
            .first()
        )
        assert action.payload_json["monitor_series"] is True

        class RefillSonarr(FakeSonarr):
            def monitor_series(self, series_id):
                self.calls.append(("monitor_series", series_id))
                return True

            def monitor_episode(self, _episode_id):
                return True

            def monitor_season(self, _series_id, _season):
                return True

            def commands(self):
                return []

            def season_search(self, series_id, season):
                self.calls.append(("search", series_id, season))

        refill = RefillSonarr()
        monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: refill)
        assert execute_action(action.id) == "succeeded"
        assert ("monitor_series", 77) in refill.calls
        assert media.arr_monitored is True


def test_untouched_unmonitored_shows_are_still_left_alone(app):
    with app.app_context():
        media, parts = _show()
        media.arr_monitored = False
        parts[(2, 1)].has_file = False
        db.session.commit()
        _watched(parts[(1, 4)], days_ago=1)
        assert reconcile_acquisition() == 0


def test_reset_button_queues_the_reset_for_the_worker(app, client, monkeypatch):
    """The page must not run the deletions itself: a long show outlasted the web
    server's request timeout and was cut off after one file."""
    from curatarr.lifecycle import resume_approved
    from curatarr.models import PurgeCandidate

    with app.app_context():
        media, _parts = _show()
        sonarr = _clients(monkeypatch)
        page = client.post(f"/titles/{media.id}/reset", follow_redirects=True).text
        assert "started" in page
        assert not [c for c in sonarr.calls if c[0] == "delete"]
        candidate = (
            db.session.query(PurgeCandidate).filter_by(media_identity_id=media.id).one()
        )
        assert candidate.state == "APPROVED"
        resume_approved()  # what the worker runs every cycle
        db.session.refresh(candidate)
        assert candidate.state == "COMPLETED"
        assert sorted(c[1] for c in sonarr.calls if c[0] == "delete") == [
            1004,
            2001,
            2002,
        ]
