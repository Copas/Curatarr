from datetime import timedelta

from test_acquisition_reconcile import _show

from curatarr import db, missing_keep
from curatarr.models import LifecycleAction, MediaIdentity, utcnow


class _Sonarr:
    def __init__(self):
        self.episode_searches, self.season_searches = [], []
        self.series_monitored, self.episodes_monitored = [], []

    def monitor_series(self, series_id):
        self.series_monitored.append(series_id)
        return True

    def monitor_episode(self, episode_id):
        self.episodes_monitored.append(episode_id)
        return True

    def queue(self):
        return {"records": []}

    def commands(self):
        return []

    def season_search(self, series_id, season):
        self.season_searches.append((series_id, season))

    def episode_search(self, episode_ids):
        self.episode_searches.append(list(episode_ids))


def test_lists_shows_missing_always_keep_episodes(app):
    _show("Complete", [(1, e, True) for e in range(1, 6)])
    gappy, _ = _show(
        "Gappy",
        [(1, 1, True), (1, 2, False), (1, 3, False), (1, 4, True), (1, 5, True)]
        + [(1, 6, False), (2, 1, False)],  # beyond the minimum: not listed
        monitored=False,
    )
    _show("Not Aired", [(1, 1, False, utcnow() + timedelta(days=3))])
    _show("Nightly", [(1, 1, False)], series_type="daily")
    (row,) = missing_keep.rows()
    assert row["media"].id == gappy.id
    assert row["codes"] == "S01E02, S01E03"
    assert row["unmonitored"] and not row["searched"] and not row["waiting"]
    assert missing_keep.count() == 1


def test_search_once_asks_for_just_the_missing_episodes(app, client, monkeypatch):
    sonarr = _Sonarr()
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: sonarr)
    media, _ = _show(
        "Gappy",
        [(1, 1, True), (1, 2, False), (1, 3, False), (1, 4, True), (1, 5, True)],
        monitored=False,
    )
    page = client.get("/chapters/problems").text
    assert "Missing S01E02, S01E03." in page and "nothing will fetch them" in page
    page = client.post(
        f"/chapters/problems/show/{media.id}/search", follow_redirects=True
    ).text
    assert "Asked Sonarr to search" in page
    # Sonarr won't search an unmonitored show; only the missing episodes are monitored.
    assert sonarr.series_monitored == [media.sonarr_id]
    assert sorted(sonarr.episodes_monitored) == [1002, 1003]
    # Just these episodes: a season search would grab whole-season packs.
    assert sonarr.episode_searches == [[1002, 1003]] and sonarr.season_searches == []
    (row,) = missing_keep.rows()
    assert row["waiting"] and missing_keep.count() == 0
    # Pressing it again is a new request, not the earlier one returned.
    client.post(f"/chapters/problems/show/{media.id}/search")
    assert len(sonarr.episode_searches) == 2
    assert (
        db.session.query(LifecycleAction)
        .filter_by(media_identity_id=media.id, action_type="sonarr_season_search")
        .count()
        == 2
    )


def test_unanswered_search_is_listed_again(app):
    media, _ = _show("Gappy", [(1, 1, False)], monitored=False)
    db.session.add(
        LifecycleAction(
            idempotency_key="old-search",
            action_type="sonarr_season_search",
            state="SUCCEEDED",
            media_identity_id=media.id,
            reason_text="Searched",
            created_at=utcnow() - timedelta(days=5),
        )
    )
    db.session.commit()
    (row,) = missing_keep.rows()
    assert row["searched"] and not row["waiting"] and missing_keep.count() == 1


def test_leave_them_missing_hides_until_another_gap(app, client):
    media, parts = _show("Gappy", [(1, 1, False), (1, 2, True)], monitored=False)
    client.post(f"/chapters/problems/show/{media.id}/ignore")
    assert missing_keep.rows() == []
    parts[(1, 2)].has_file = False  # a new gap
    db.session.commit()
    (row,) = missing_keep.rows()
    assert row["codes"] == "S01E01, S01E02"


def test_overview_offers_search_once(app, client):
    media, _ = _show("Gappy", [(1, 1, False)], monitored=False)
    page = client.get("/").text
    assert "Gappy" in page and "Missing S01E01, which it always keeps" in page
    assert f"/chapters/problems/show/{media.id}/search" in page


def test_gone_shows_are_not_listed(app):
    media, _ = _show("Removed", [(1, 1, False)])
    db.session.get(MediaIdentity, media.id).missing_since = utcnow()
    db.session.commit()
    assert missing_keep.rows() == []
