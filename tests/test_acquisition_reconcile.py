from datetime import timedelta

from curatarr import db
from curatarr.models import (
    EpisodeUserState,
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    TitleOverride,
    utcnow,
)
from curatarr.services import ACQUISITION_SEARCHES_PER_RUN, reconcile_acquisition

AIRED = utcnow() - timedelta(days=400)


def _library():
    library = db.session.query(Library).first()
    if not library:
        library = Library(jellyfin_library_id="tv", name="Shows", media_type="tv")
        db.session.add(library)
        db.session.flush()
    return library


def _show(title, episodes, *, monitored=True, series_type="standard", sonarr_id=None):
    """episodes: (season, episode, has_file[, air_date]) tuples."""
    media = MediaIdentity(
        library_id=_library().id,
        media_type="series",
        title=title,
        jellyfin_id=f"{title}-id",
        sonarr_id=sonarr_id or abs(hash(title)) % 100000,
        tvdb_id=abs(hash(title)) % 100000,
        arr_monitored=monitored,
        series_type=series_type,
        added_at=AIRED,
    )
    db.session.add(media)
    db.session.flush()
    parts = {}
    for row in episodes:
        season, episode, has_file = row[:3]
        air = row[3] if len(row) > 3 else AIRED
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=season,
            episode_number=episode,
            sonarr_episode_id=season * 1000 + episode,
            has_file=has_file,
            air_date=air,
        )
        db.session.add(part)
        parts[(season, episode)] = part
    db.session.commit()
    return media, parts


def _watched(part, days_ago=5):
    db.session.add(
        EpisodeUserState(
            media_part_id=part.id,
            jellyfin_user_id="viewer",
            played=True,
            last_played_at=utcnow() - timedelta(days=days_ago),
        )
    )
    db.session.commit()


def _requests(media=None):
    query = db.session.query(LifecycleAction).filter_by(
        action_type="sonarr_season_search"
    )
    if media:
        query = query.filter_by(media_identity_id=media.id)
    return query.order_by(LifecycleAction.created_at).all()


def test_missing_always_keep_episodes_are_requested(app):
    media, _ = _show("Gap Show", [(1, e, e > 3) for e in range(1, 6)] + [(2, 1, False)])
    assert reconcile_acquisition() == 1
    (action,) = _requests(media)
    # First 3 episodes of Season 1 only: not E4/E5 (present) or Season 2.
    assert action.payload_json["episode_ids"] == [1001, 1002, 1003]
    assert action.payload_json["search_now"] is True
    assert reconcile_acquisition() == 0  # never queued twice


def test_unmonitored_daily_and_opted_out_shows_are_left_alone(app):
    _show("Switched Off", [(1, 1, False)], monitored=False)
    _show("Talk Nightly", [(1, 1, False)], series_type="daily")
    opted_out, _ = _show("Late Show", [(1, 1, False)])
    db.session.add(
        TitleOverride(
            media_identity_id=opted_out.id,
            override_json={"fill_minimum_footprint": False},
        )
    )
    db.session.commit()
    assert reconcile_acquisition() == 0
    assert _requests() == []


def test_next_season_is_caught_up_for_shows_being_watched(app):
    media, parts = _show(
        "Caught Up", [(1, 1, True), (1, 2, True), (1, 3, True), (2, 1, False)]
    )
    _watched(parts[(1, 3)])
    reconcile_acquisition()
    seasons = sorted(a.payload_json["season"] for a in _requests(media))
    assert seasons == [2]


def test_abandoned_shows_do_not_get_new_seasons(app):
    _media, parts = _show(
        "Abandoned", [(1, 1, True), (1, 2, True), (1, 3, True), (2, 1, False)]
    )
    _watched(parts[(1, 3)], days_ago=400)
    assert reconcile_acquisition() == 0


def test_searches_are_spread_over_runs(app):
    for index in range(ACQUISITION_SEARCHES_PER_RUN + 2):
        _show(f"Show {index}", [(1, 1, False)], sonarr_id=index + 1)
    reconcile_acquisition()
    first = _requests()
    searching = [a for a in first if a.payload_json["search_now"]]
    assert len(searching) == ACQUISITION_SEARCHES_PER_RUN
    # The rest are monitored now and searched in the next run.
    reconcile_acquisition()
    searched_titles = {
        a.media_identity_id for a in _requests() if a.payload_json["search_now"]
    }
    assert len(searched_titles) == ACQUISITION_SEARCHES_PER_RUN + 2


def test_undated_episodes_are_monitored_but_not_searched(app):
    media, _ = _show("Announced", [(1, 1, False, None), (1, 2, False, None)])
    reconcile_acquisition()
    (action,) = _requests(media)
    assert action.payload_json["episode_ids"] == [1001, 1002]
    assert action.payload_json["search_now"] is False


def test_requests_explain_who_watched_what_and_what_happens(app, client):
    from curatarr.services import ingest_event, process_pending_events, set_setting

    set_setting("jellyfin_user_names", {"viewer": "Kelden"})
    media, parts = _show(
        "XYZ",
        [(1, 1, True), (1, 2, True), (1, 3, True), (2, 1, False), (2, 2, False, None)],
    )
    parts[(1, 1)].jellyfin_id = "xyz-1-1"
    db.session.commit()
    ingest_event(
        {
            "event_id": "kelden-1",
            "event_type": "item_played",
            "item_external_id": "xyz-1-1",
            "series_external_id": media.jellyfin_id,
            "season_number": 1,
            "episode_number": 1,
            "user_external_id": "viewer",
            "played": True,
        }
    )
    process_pending_events()
    (action,) = _requests(media)
    assert action.reason_text == (
        "Kelden finished S01E01 of XYZ, so Curatarr is searching for Season 2."
    )
    page = client.get("/").text
    assert "Downloads requested" in page
    assert (
        "Kelden finished S01E01 of XYZ, so Curatarr is searching for Season 2." in page
    )


def test_catch_up_and_always_keep_messages(app):
    from curatarr.services import set_setting

    set_setting("jellyfin_user_names", {"viewer": "Kelden"})
    watching, parts = _show(
        "Catch Up", [(1, 1, True), (1, 2, True), (1, 3, True), (2, 1, False, None)]
    )
    _watched(parts[(1, 2)])
    missing, _ = _show("Gaps", [(1, 1, False), (1, 2, False), (1, 3, True)])
    reconcile_acquisition()
    (catch_up,) = _requests(watching)
    assert catch_up.reason_text == (
        "Kelden finished S01E02 of Catch Up, the latest watched in Season 1, so "
        "Curatarr is monitoring Season 2 so Sonarr downloads it when it airs."
    )
    (fill,) = _requests(missing)
    assert fill.reason_text == (
        "Gaps is missing episodes it always keeps, so Curatarr is searching "
        "for S01E01–S01E02."
    )
