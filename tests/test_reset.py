"""Title page "Reset to minimum": trim a show to its always-keep episodes as if
nobody had watched it, through the normal approved-deletion path."""

import re
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


def test_shows_by_size_lists_shows_largest_first_with_reset_links(app, client):
    with app.app_context():
        media, _parts = _show(dry_run=True)
        small = MediaIdentity(
            library_id=media.library_id,
            media_type="series",
            title="Small Show",
            jellyfin_id="small",
            sonarr_id=78,
            tvdb_id=778,
            added_at=utcnow(),
        )
        db.session.add(small)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=small.id,
                kind="episode",
                season_number=1,
                episode_number=1,
                sonarr_episode_id=9101,
                has_file=True,
                size_bytes=50,
            )
        )
        db.session.commit()
        page = client.get("/shows").text
        assert "Shows by size" in page
        assert page.index("Reset Show") < page.index(
            "Small Show"
        )  # 600 bytes before 50
        assert f"/titles/{media.id}/reset" in page  # 300 bytes outside the minimum
        assert "At minimum" in page  # Small Show has only its always-keep episode
        filtered = client.get(f"/shows?library={media.library_id}").text
        assert "Reset Show" in filtered


def test_daily_shows_are_flagged_before_a_reset(app, client):
    with app.app_context():
        media, _parts = _show(dry_run=True)
        media.series_type = "daily"
        db.session.commit()
        assert "Daily show" in client.get("/shows").text
        assert "is a daily show" in client.get(f"/titles/{media.id}/reset").text


def _unwatched_show(title, *, added_days_ago=200, daily=False, library_id):
    media = MediaIdentity(
        library_id=library_id,
        media_type="series",
        title=title,
        jellyfin_id=title,
        sonarr_id=abs(hash(title)) % 9000 + 100,
        tvdb_id=abs(hash(title)) % 90000 + 1000,
        series_type="daily" if daily else "standard",
        added_at=utcnow() - timedelta(days=added_days_ago),
    )
    db.session.add(media)
    db.session.flush()
    for index, (season, episode) in enumerate(
        [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1)], start=1
    ):
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=2026 if daily else season,
                episode_number=index if daily else episode,
                sonarr_episode_id=abs(hash((title, season, episode))) % 900000,
                arr_file_id=abs(hash((title, "f", season, episode))) % 900000,
                has_file=True,
                size_bytes=100,
            )
        )
    db.session.commit()
    return media


def test_unwatched_plan_picks_never_started_shows_and_explains_skips(app):
    from curatarr.services import unwatched_reset_plan

    with app.app_context():
        watched, parts = _show()  # "Reset Show"
        _watched(parts[(1, 1)], days_ago=3)
        lib = watched.library_id
        old = _unwatched_show("Old Unwatched", library_id=lib)
        _unwatched_show("Brand New", added_days_ago=5, library_id=lib)
        _unwatched_show("Nightly", daily=True, library_id=lib)
        guarded = _unwatched_show("Guarded", library_id=lib)
        db.session.add(
            TitleOverride(
                media_identity_id=guarded.id, override_json={"never_purge": True}
            )
        )
        db.session.commit()
        plan, skipped = unwatched_reset_plan(30)
        assert [r["media"].title for r in plan] == ["Old Unwatched"]
        reasons = {r["media"].title: r["reason"] for r in skipped}
        assert reasons["Brand New"].startswith("Added ")
        assert reasons["Nightly"].startswith("Daily show")
        assert reasons["Guarded"] == "Never Purge is on"
        assert "Reset Show" not in reasons  # watched shows are not listed at all
        assert plan[0]["frees"] == 200 and plan[0]["reset_files"] == 2
        plan, _ = unwatched_reset_plan(0)  # no recent-add exemption
        assert {r["media"].title for r in plan} == {"Old Unwatched", "Brand New"}
        assert old.id in {r["media"].id for r in plan}


def test_bulk_reset_requires_the_exact_phrase_and_an_unchanged_list(
    app, client, monkeypatch
):
    from curatarr.lifecycle import resume_approved
    from curatarr.models import PurgeCandidate

    with app.app_context():
        lib = _show(dry_run=False)[0].library_id
        db.session.query(MediaIdentity).delete()
        db.session.commit()
        a = _unwatched_show("Alpha", library_id=lib)
        b = _unwatched_show("Beta", library_id=lib)
        page = client.get("/shows/reset-unwatched").text
        assert "Reset all unwatched shows?" in page and "reset 2 shows" in page
        assert "Not a dry run" in page
        signature = re.search(r'name="signature" value="([^"]+)"', page).group(1)
        client.post(
            "/shows/reset-unwatched",
            data={"confirm": "yes", "signature": signature, "skip_days": 30},
        )
        assert (
            db.session.query(PurgeCandidate).count() == 0
        )  # wrong phrase: nothing queued
        client.post(
            "/shows/reset-unwatched",
            data={"confirm": "reset 2 shows", "signature": "stale", "skip_days": 30},
        )
        assert db.session.query(PurgeCandidate).count() == 0  # list changed: refused
        response = client.post(
            "/shows/reset-unwatched",
            data={"confirm": "Reset 2 Shows", "signature": signature, "skip_days": 30},
            follow_redirects=True,
        )
        assert "Started 2 resets" in response.text
        states = {
            c.media_identity_id: c.state for c in db.session.query(PurgeCandidate)
        }
        assert states == {a.id: "APPROVED", b.id: "APPROVED"}

        class AnySonarr(FakeSonarr):
            def request(self, _method, path):
                media = a if str(a.sonarr_id) in path else b
                return {"tvdbId": media.tvdb_id}

            def episodes(self, series_id):
                media = a if series_id == a.sonarr_id else b
                return [
                    {
                        "id": p.sonarr_episode_id,
                        "hasFile": True,
                        "episodeFileId": p.arr_file_id,
                    }
                    for p in media.parts
                ]

            def episode_files(self, series_id):
                media = a if series_id == a.sonarr_id else b
                return [{"id": p.arr_file_id, "size": 100} for p in media.parts]

        class AnyJellyfin:
            def item(self, item_id):
                media = a if item_id == a.jellyfin_id else b
                return {"ProviderIds": {"Tvdb": str(media.tvdb_id)}}

        sonarr = AnySonarr()
        monkeypatch.setattr(
            "curatarr.lifecycle.client",
            lambda kind: AnyJellyfin() if kind == "jellyfin" else sonarr,
        )
        resume_approved()
        assert {c.state for c in db.session.query(PurgeCandidate)} == {"COMPLETED"}
        assert (
            len([c for c in sonarr.calls if c[0] == "delete"]) == 4
        )  # S1E4 and S2E1 of each
