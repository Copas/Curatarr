"""When favourites were made (favourite ribbons' NEW), and favourite episodes."""

from test_enrichment import _movie

from curatarr import db
from curatarr.models import EpisodeUserState, MediaIdentity, MediaPart, UserMediaState
from curatarr.services import (
    _save_episode_favourites,
    ingest_event,
    process_pending_events,
    set_setting,
)


def _favourite(media, favourite, first_look=False, n=1):
    ingest_event(
        {
            "event_id": f"fav-{media.id}-{favourite}-{n}",
            "event_type": "favorite_changed",
            "item_external_id": media.jellyfin_id,
            "user_external_id": "u1",
            "favorite": favourite,
            "first_look": first_look,
        }
    )
    process_pending_events()
    return db.session.query(UserMediaState).filter_by(media_identity_id=media.id).one()


def test_a_new_favourite_is_dated_and_one_found_on_the_first_look_isnt(app):
    _library, film, _part = _movie()
    state = _favourite(film, True, first_look=True)
    assert state.favorite and state.favorite_since is None  # old, not new
    state = _favourite(film, False)
    assert not state.favorite and state.favorite_since is None
    state = _favourite(film, True, n=2)
    assert state.favorite and state.favorite_since is not None


def test_favourite_episodes_are_kept_in_step_with_jellyfin(app):
    library, _film, _part = _movie()
    show = MediaIdentity(
        library_id=library.id, media_type="series", title="Show", jellyfin_id="show"
    )
    db.session.add(show)
    db.session.flush()
    parts = []
    for number in (1, 2):
        part = MediaPart(
            media_identity_id=show.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
            jellyfin_id=f"ep-{number}",
        )
        db.session.add(part)
        parts.append(part)
    db.session.commit()
    liked = {(show.id, "u1"): [{"Id": "ep-1"}]}
    # The first time, they're old favourites.
    _save_episode_favourites(liked, {"u1"}, first_look=False)
    state = db.session.query(EpisodeUserState).one()
    assert state.favorite and state.favorite_since is None
    # One favourited since is new; one no longer listed isn't a favourite.
    set_setting("episode_favourites_synced", "2026-01-01T00:00:00+00:00")
    _save_episode_favourites(
        {(show.id, "u1"): [{"Id": "x", "ParentIndexNumber": 1, "IndexNumber": 2}]},
        {"u1"},
        first_look=False,
    )
    states = {s.media_part_id: s for s in db.session.query(EpisodeUserState).all()}
    assert not states[parts[0].id].favorite
    assert states[parts[1].id].favorite and states[parts[1].id].favorite_since
