"""What people will watch next is got ready first."""

from datetime import timedelta

from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import db, enrichment, up_next
from curatarr.models import MediaIdentity, MediaPart, utcnow


class FakeJellyfin:
    def __init__(self, next_up=(), resume=()):
        self._next_up, self._resume = list(next_up), list(resume)

    def users(self):
        return [{"Id": "u1"}]

    def next_up(self, user_id):
        return [{"Id": i} for i in self._next_up]

    def resume(self, user_id):
        return [{"Id": i} for i in self._resume]


def _show(library_id, episodes):
    show = MediaIdentity(library_id=library_id, media_type="series", title="Show")
    db.session.add(show)
    db.session.flush()
    parts = []
    for number in range(1, episodes + 1):
        part = MediaPart(
            media_identity_id=show.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
            file_path=f"/tv/Show/S01E{number:02}.mkv",
            jellyfin_id=f"{number:032x}",
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
    episodes = _show(library.id, 6)
    db.session.commit()
    jellyfin = FakeJellyfin(next_up=[f"{2:032x}"], resume=["f" * 32])
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    assert up_next.refresh() == 4  # E02, the two after it, and the film
    assert set(up_next.part_ids()) == {
        episodes[1].id,
        episodes[2].id,
        episodes[3].id,
        film.id,
    }
    for part in episodes:  # all queued; E05 and E06 first
        enrichment.request(part, "generate")
    for part in reversed(episodes[:4]):
        enrichment.state_for(part).requested_at = utcnow() + timedelta(hours=1)
    db.session.commit()
    due = [s.media_part_id for s in enrichment._due_jobs(10, scans=False)]
    assert set(due[:3]) == {episodes[1].id, episodes[2].id, episodes[3].id}
    # Asked again only every ten minutes.
    jellyfin._next_up = []
    assert up_next.refresh() == 4
