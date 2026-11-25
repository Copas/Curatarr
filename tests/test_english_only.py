from curatarr import db, english_only
from curatarr.models import (
    ChapterState,
    Library,
    MediaIdentity,
    MediaPart,
    UserMediaState,
)
from curatarr.services import set_setting, setting

NOT_ENGLISH = {
    "id": 7,
    "name": "Language: Not English",
    "specifications": [
        {
            "implementation": "LanguageSpecification",
            "negate": True,
            "fields": [{"name": "value", "value": 1}],
        }
    ],
}


def test_a_profile_blocks_other_languages_when_it_scores_them_down():
    formats = {7: NOT_ENGLISH}
    assert english_only.blocks_other_languages(
        {"formatItems": [{"format": 7, "score": -1000}]}, formats
    )
    assert not english_only.blocks_other_languages(
        {"formatItems": [{"format": 7, "score": 0}]}, formats
    )
    assert not english_only.blocks_other_languages({"formatItems": []}, formats)


class FakeRadarr:
    def __init__(self):
        self.excluded = []

    def custom_formats(self):
        return [NOT_ENGLISH]

    def movie(self, movie_id):
        return {"qualityProfileId": 1 if movie_id < 100 else 2, "year": 2026}

    def quality_profile(self, profile_id):
        score = -1000 if profile_id == 1 else 0  # 2: a foreign-films profile
        return {"formatItems": [{"format": 7, "score": score}]}

    def add_exclusion(self, tmdb_id, title, year):
        self.excluded.append(tmdb_id)


def _film(library, title, radarr_id, *, auto=True, watched=False):
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title=title,
        radarr_id=radarr_id,
        tmdb_id=str(radarr_id),
    )
    db.session.add(media)
    db.session.flush()
    part = MediaPart(media_identity_id=media.id, kind="movie_file", has_file=True)
    db.session.add(part)
    db.session.flush()
    state = ChapterState(media_part_id=part.id, media_identity_id=media.id)
    state.dub_result = {
        "verdict": "no_english",
        "dismissed": True,
        "replace_auto": auto,
        "no_better": {"checked": 3},
    }
    db.session.add(state)
    if watched:
        db.session.add(
            UserMediaState(
                media_identity_id=media.id, jellyfin_user_id="sam", play_count=1
            )
        )
    return media


def test_films_with_no_english_copy_are_deleted_and_stay_off_the_lists(
    app, monkeypatch
):
    library = Library(jellyfin_library_id="films", name="Films", media_type="movies")
    db.session.add(library)
    db.session.flush()
    leaving = _film(library, "Leaving", 1)
    _film(library, "Foreign on purpose", 101)  # its profile allows them
    _film(library, "Watched", 2, watched=True)
    _film(library, "Chosen by hand", 3, auto=False)
    db.session.commit()
    radarr = FakeRadarr()
    monkeypatch.setattr("curatarr.services.client", lambda _kind: radarr)
    deleted = []

    def delete(media, candidate=None):
        deleted.append(media.title)
        return "deleted"

    monkeypatch.setattr(english_only, "_delete", delete)
    assert english_only.sweep() == 0  # off by default
    set_setting("auto_fixes", {"no_english": True})
    db.session.commit()
    assert english_only.sweep() == 1
    assert deleted == ["Leaving"]
    assert radarr.excluded == [1]
    assert english_only.sweep() == 0  # each looked at once
    # Never Purge (or the like) refuses: still kept off the lists
    refused = _film(library, "Never purge", 4)
    db.session.commit()

    def refuse(media, candidate=None):
        raise ValueError(f"Never Purge is on for {media.title}.")

    monkeypatch.setattr(english_only, "_delete", refuse)
    assert english_only.sweep() == 0
    assert radarr.excluded == [1, 4] and refused.id
    state = db.session.query(ChapterState).filter_by(media_identity_id=leaving.id).one()
    assert state.dub_result["english_only"]["outcome"] == "deleted"
    assert setting("auto_fixes")["no_english"]


def test_films_it_marked_leaving_soon_before_are_deleted_now(app, monkeypatch):
    from curatarr.models import PurgeCandidate

    library = Library(jellyfin_library_id="films", name="Films", media_type="movies")
    db.session.add(library)
    db.session.flush()
    media = _film(library, "Marked earlier", 1)
    other = _film(library, "Left by a rule", 2)
    for film, text in (
        (media, f"Marked Leaving Soon from {english_only.WHERE}."),
        (other, "No playback within 365 days."),
    ):
        db.session.add(
            PurgeCandidate(
                media_identity_id=film.id,
                state="LEAVING_SOON",
                reason_code="requested" if film is media else "inactivity",
                reason_text=text,
                reclaimable_bytes=1,
            )
        )
    set_setting("auto_fixes", {"no_english": True})
    db.session.commit()
    monkeypatch.setattr(english_only, "due", list)
    assert english_only.sweep() == 1
    states = {c.media.title: c.state for c in db.session.query(PurgeCandidate).all()}
    assert states == {"Marked earlier": "APPROVED", "Left by a rule": "LEAVING_SOON"}
