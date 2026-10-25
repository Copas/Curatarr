"""What people will watch next is got ready first."""

from datetime import timedelta

from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import db, enrichment, up_next
from curatarr.models import MediaIdentity, MediaPart, utcnow


class FakeJellyfin:
    def __init__(self, next_up=(), resume=(), playing=()):
        self._next_up, self._resume = list(next_up), list(resume)
        self.playing = list(playing)  # (user, item) pairs

    def sessions(self):
        return [{}] + [
            {"UserId": user, "NowPlayingItem": {"Id": item}}
            for user, item in self.playing
        ]

    def users(self):
        return [{"Id": "u1"}]

    def next_up(self, user_id):
        return [{"Id": i} for i in self._next_up]

    def resume(self, user_id):
        return [{"Id": i} for i in self._resume]


def _show(library_id, episodes, seasons=1):
    show = MediaIdentity(library_id=library_id, media_type="series", title="Show")
    db.session.add(show)
    db.session.flush()
    parts = []
    for index in range(episodes * seasons):
        season, number = index // episodes + 1, index % episodes + 1
        part = MediaPart(
            media_identity_id=show.id,
            kind="episode",
            season_number=season,
            episode_number=number,
            has_file=True,
            file_path=f"/tv/Show/S{season:02}E{number:02}.mkv",
            jellyfin_id=f"{index + 1:032x}",
        )
        db.session.add(part)
        parts.append(part)
    db.session.commit()
    return parts


def test_the_next_episodes_and_resumed_films_jump_the_queue(
    app,
    media_env,  # noqa: F811
    monkeypatch,
):
    library, media, film = _movie()
    media.jellyfin_id = "f" * 32  # a film's Jellyfin id is the title's
    episodes = _show(library.id, 9)
    db.session.commit()
    jellyfin = FakeJellyfin(next_up=[f"{2:032x}"], resume=["f" * 32])
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    assert up_next.refresh() == 7  # E02, the five after it, and the film
    assert set(up_next.part_ids()) == {p.id for p in episodes[1:7]} | {film.id}
    for part in episodes:  # all queued; E08 and E09 first
        enrichment.request(part, "generate")
    for part in reversed(episodes[:7]):
        enrichment.state_for(part).requested_at = utcnow() + timedelta(hours=1)
    db.session.commit()
    due = [s.media_part_id for s in enrichment._due_jobs(10, scans=False)]
    assert set(due[:6]) == {p.id for p in episodes[1:7]}
    # Asked again only every ten minutes.
    jellyfin._next_up = []
    assert up_next.refresh() == 7


def test_pressing_play_moves_it_and_whats_next_up_at_once(
    app,
    media_env,  # noqa: F811
    monkeypatch,
):
    """Someone started an episode whose seek index Roku couldn't use: it and
    the episodes after it, into the next season, go first straight away,
    not at the next ten-minute look."""
    library, _media, _film = _movie()
    episodes = _show(library.id, 4, seasons=2)  # S01E01-04, S02E01-04
    jellyfin = FakeJellyfin()
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    assert up_next.refresh() == 0
    jellyfin.playing = [("u2", f"{3:032x}")]  # S01E03
    up_next.refresh()  # within the ten minutes: what's playing still counts
    playing = up_next.part_ids()
    assert playing[0] == episodes[2].id
    assert set(playing) == {p.id for p in episodes[2:8]}  # E03, E04, S02E01-04
    assert set(up_next.part_ids(users=["u2"])) == set(playing)
    assert up_next.part_ids(users=["u1"]) == []
    jellyfin.playing = []
    up_next.refresh()
    assert up_next.part_ids() == []
