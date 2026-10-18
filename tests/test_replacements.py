"""A replaced film is watched until its new copy arrives."""

from datetime import timedelta

from test_request_removal import _film

from curatarr import db, replacements
from curatarr.models import LifecycleAction, utcnow
from curatarr.services import setting


class Radarr:
    def __init__(self, monitored=False):
        self.monitored = monitored
        self.has_file = False
        self.queued = False
        self.searches = 0

    def movie(self, _movie_id):
        return {"monitored": self.monitored, "hasFile": self.has_file}

    def set_monitored(self, _movie_id, monitored):
        self.monitored = monitored

    def queue(self):
        return [{"movieId": 42}] if self.queued else []

    def search_movie(self, _movie_id):
        self.searches += 1


def _audits(kind):
    return db.session.query(LifecycleAction).filter_by(action_type=kind).count()


def test_an_unmonitored_film_is_monitored_and_searched_until_it_arrives(
    app, monkeypatch
):
    radarr = Radarr(monitored=False)
    monkeypatch.setattr("curatarr.services.client", lambda kind: radarr)
    with app.app_context():
        media = _film()
        replacements.watch(media, radarr, radarr.movie(42))
        assert radarr.monitored  # so a failed download is retried
        now = utcnow()
        assert replacements.sweep(now) == 0  # just searched
        radarr.queued = True
        assert replacements.sweep(now + timedelta(hours=7)) == 0  # downloading
        radarr.queued = False  # the download failed
        assert replacements.sweep(now + timedelta(hours=7)) == 1
        radarr.has_file = True
        assert replacements.sweep(now + timedelta(hours=8)) == 0
        assert not radarr.monitored  # back as it was
        assert setting(replacements.KEY) == {}
        assert _audits("replacement_arrived") == 1


def test_gives_up_after_three_searches_and_says_so(app, monkeypatch):
    radarr = Radarr(monitored=True)
    monkeypatch.setattr("curatarr.services.client", lambda kind: radarr)
    with app.app_context():
        media = _film()
        replacements.watch(media, radarr, radarr.movie(42))
        now = utcnow()
        for hours in (7, 14, 21):
            replacements.sweep(now + timedelta(hours=hours))
        assert radarr.searches == 2 and setting(replacements.KEY) == {}
        assert _audits("replacement_failed") == 1
        assert radarr.monitored  # Radarr takes one if it ever appears
