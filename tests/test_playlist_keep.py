"""Titles in a Jellyfin playlist are kept from automatic cleanup, not from
being removed by hand (playlist_keep.py)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from test_acquisition_reconcile import _show
from test_enrichment import _movie

from curatarr import curation, db, playlist_keep
from curatarr.policy import CandidateInput, effective_policy, eligible
from curatarr.services import set_setting, utcnow

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class Jellyfin:
    """Two playlists: one someone made (a film, an episode of a show) and
    Curatarr's own Picks playlist, readable only as their owners."""

    def __init__(self, show_id):
        self.lists = {
            "watch": (
                "Sam's watchlist",
                "u2",
                [
                    {"Id": "jf-film", "Type": "Movie"},
                    {"Id": "ep-1", "Type": "Episode", "SeriesId": show_id},
                ],
            ),
            "picks": ("Picks for Alex", "u1", [{"Id": "picked", "Type": "Movie"}]),
        }

    def playlists(self):
        return [{"Id": key, "Name": name} for key, (name, _o, _i) in self.lists.items()]

    def users(self):
        return [{"Id": "u1"}, {"Id": "u2"}]

    def playlist_items(self, playlist_id, user_id):
        _name, owner, items = self.lists[playlist_id]
        return items if user_id == owner else []


def test_a_film_or_show_in_a_playlist_is_found_but_not_curatarrs_picks(
    app, monkeypatch
):
    _library, film, _part = _movie()
    show, _parts = _show("Tasks", [(1, 1, True)])
    picked, _p = _show("Picked", [(1, 1, True)])
    picked.media_type, picked.jellyfin_id = "movie", "picked"
    set_setting("picks", {"u1": {"playlist": "picks"}})
    db.session.commit()
    monkeypatch.setattr(
        "curatarr.services.client", lambda _kind: Jellyfin(show.jellyfin_id)
    )
    assert playlist_keep.sweep(now=utcnow()) == 2
    kept = playlist_keep.kept()
    assert kept == {film.id: ["Sam's watchlist"], show.id: ["Sam's watchlist"]}
    curation._cache.clear()
    assert curation.signals(film)["playlists"] == ["Sam's watchlist"]
    assert curation.signals(picked)["playlists"] == []
    # turned off: nothing is kept for being in a playlist
    set_setting("curation", {"playlist_keeps": False})
    db.session.commit()
    curation._cache.clear()
    assert curation.signals(film)["playlists"] == []


def test_automatic_cleanup_skips_a_title_in_a_playlist():
    policy, _ = effective_policy("movie")
    old = CandidateInput("film", "movie", NOW - timedelta(days=400), None, 100)
    assert eligible(old, policy, NOW)
    assert not eligible(replace(old, in_playlist=True), policy, NOW)


def test_the_title_page_says_it_is_kept(app, client):
    _library, film, _part = _movie()
    set_setting(
        playlist_keep.KEY,
        {"titles": {film.id: ["Sam's watchlist"]}, "at": utcnow().isoformat()},
    )
    db.session.commit()
    curation._cache.clear()
    page = client.get(f"/titles/{film.id}").text
    assert "In a playlist: kept from automatic cleanup" in page
    assert "Sam&#39;s watchlist" in page or "Sam's watchlist" in page
