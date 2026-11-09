from datetime import timedelta

from curatarr import db, last_call
from curatarr.artwork import LAST_CALL_LABEL, badge_label
from curatarr.lifecycle import reconcile_candidates, review_candidate
from curatarr.models import (
    Library,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    UserMediaState,
    utcnow,
)
from curatarr.services import set_setting


def _films(count=5, *, library_type="movies", days_old=400):
    library = Library(
        jellyfin_library_id=library_type, name="Films", media_type=library_type
    )
    db.session.add(library)
    db.session.flush()
    films = []
    for n in range(count):
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Film {n}",
            radarr_id=n + 1,
            jellyfin_id=f"{n:032x}",
            added_at=utcnow() - timedelta(days=days_old + n),  # higher n: older
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=1_000_000,
            )
        )
        films.append(media)
    db.session.commit()
    return library, films


def _on(**extra):
    set_setting("curation", {"last_call": True, "last_call_per_week": 2} | extra)
    db.session.commit()


def _in_last_call():
    return {
        c.media.title
        for c in db.session.query(PurgeCandidate).filter_by(state="LAST_CALL")
    }


def test_a_few_unwatched_films_a_week_oldest_first(app):
    _films()
    assert last_call.sweep() == {"leaving": 0, "added": 0}  # off by default
    _on()
    now = utcnow()
    assert last_call.sweep(now)["added"] == 2
    assert _in_last_call() == {"Film 4", "Film 3"}  # the oldest two
    assert last_call.sweep(now + timedelta(hours=2))["added"] == 0  # this week's
    assert last_call.sweep(now + timedelta(days=8))["added"] == 2
    assert len(_in_last_call() | {"Film 4", "Film 3"}) >= 4
    label = badge_label(
        db.session.query(PurgeCandidate).filter_by(state="LAST_CALL").first()
    )
    assert label == LAST_CALL_LABEL


def test_an_upgraded_file_doesnt_make_a_film_new(app):
    _library, films = _films(1)
    films[0].parts[0].acquired_at = utcnow() - timedelta(days=3)
    db.session.commit()
    _on()
    assert last_call.sweep()["added"] == 1


def test_favourites_watched_and_new_films_are_left_out(app):
    _library, films = _films(4)
    db.session.add(
        UserMediaState(
            media_identity_id=films[3].id, jellyfin_user_id="sam", favorite=True
        )
    )
    db.session.add(
        UserMediaState(
            media_identity_id=films[2].id,
            jellyfin_user_id="sam",
            play_count=1,
            last_played_at=utcnow() - timedelta(days=5),
        )
    )
    films[1].added_at = utcnow() - timedelta(days=10)
    db.session.commit()
    _on(last_call_per_week=10)
    last_call.sweep()
    assert _in_last_call() == {"Film 0"}


def test_a_heart_keeps_it_and_time_up_moves_it_to_leaving_soon(app):
    _library, films = _films(2)
    _on()
    now = utcnow()
    last_call.sweep(now)
    assert _in_last_call() == {"Film 0", "Film 1"}
    # favourited: kept, and not picked again for a while
    db.session.add(
        UserMediaState(
            media_identity_id=films[1].id, jellyfin_user_id="sam", favorite=True
        )
    )
    db.session.commit()
    reconcile_candidates()
    assert _in_last_call() == {"Film 0"}
    # nobody kept the other: Leaving Soon once its days in Last call are up
    assert last_call.sweep(now + timedelta(days=13))["leaving"] == 0
    assert last_call.sweep(now + timedelta(days=15))["leaving"] == 1
    leaving = db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").one()
    assert leaving.media_identity_id == films[0].id
    assert leaving.media.jellyfin_id in "".join(last_call._wanted())


def test_keep_in_curatarr_isnt_undone_next_week(app):
    _films(1)
    _on()
    now = utcnow()
    last_call.sweep(now)
    candidate = db.session.query(PurgeCandidate).filter_by(state="LAST_CALL").one()
    review_candidate(candidate.id, "keep")
    assert last_call.sweep(now + timedelta(days=8))["added"] == 0


def test_only_the_chosen_libraries(app):
    library = _films(2)[0]
    _on(last_call_libraries=["another"])
    assert last_call.sweep()["added"] == 0
    _on(last_call_libraries=[library.id])
    set_setting("last_call_picked_at", None)
    db.session.commit()
    assert last_call.sweep()["added"] == 2
    # a library taken out: its films are kept
    _on(last_call_libraries=[])
    reconcile_candidates()
    assert _in_last_call() == set()


def test_household_page_saves_last_call(app, client):
    from curatarr.curation import options

    library = _films(1)[0]
    form = {
        "request_hold_days": "90",
        "list_grace_days": "7",
        "season_lead_days": "14",
        "last_call": "on",
        "last_call_days": "120",
        "last_call_per_week": "5",
        "last_call_notice_days": "21",
        "last_call_libraries_shown": "1",
        "last_call_library": library.id,
    }
    client.post("/rules/household", data=form)
    saved = options()
    assert saved["last_call"] and saved["last_call_days"] == 120
    assert saved["last_call_libraries"] is None  # every film library ticked
    assert "Last call" in client.get("/rules/household").text
