"""Payloads shaped like the Jellyfin Webhook plugin (v22) actually sends them."""

import json
import uuid

import pytest

from curatarr import db
from curatarr.models import (
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    UserMediaState,
)
from curatarr.services import (
    ingest_event,
    normalize_event,
    process_pending_events,
    set_setting,
)

SERIES = uuid.uuid4()
EPISODE = uuid.uuid4()
USER = uuid.uuid4()


@pytest.fixture
def series(app):
    library = Library(jellyfin_library_id="plugin-tv", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title="Plugin Series",
        jellyfin_id=SERIES.hex,  # the Jellyfin API returns dashless IDs
        tvdb_id=99,
        sonarr_id=9,
    )
    db.session.add(media)
    db.session.flush()
    for season, episode, stored in [(1, 1, True), (1, 2, False), (2, 1, False)]:
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=season,
                episode_number=episode,
                jellyfin_id=EPISODE.hex if (season, episode) == (1, 1) else None,
                sonarr_episode_id=season * 10 + episode,
                has_file=stored,
            )
        )
    db.session.commit()
    return media


def _template(notification, **values):
    """Every value is a string when the plugin renders a template."""
    payload = {
        "NotificationType": notification,
        "ItemId": str(EPISODE),  # dashed, as the plugin may render GUIDs
        "ItemType": "Episode",
        "SeriesId": str(SERIES),
        "SeasonNumber": "1",
        "EpisodeNumber": "1",
        "UserId": str(USER),
        "PlaybackPositionTicks": "",
        "PlayedToCompletion": "",
        "Played": "",
        "Favorite": "",
        "SaveReason": "",
    }
    payload.update(values)
    return payload


def _searches():
    return sorted(
        row.payload_json["season"]
        for row in db.session.query(LifecycleAction).filter_by(
            action_type="sonarr_season_search"
        )
    )


def test_playback_stop_to_completion_counts_as_played(series):
    ingest_event(_template("PlaybackStop", PlayedToCompletion="True"))
    process_pending_events()
    assert _searches() == [1, 2]
    state = db.session.query(UserMediaState).one()
    assert state.jellyfin_user_id == USER.hex
    assert state.completed_episode_count == 1


def test_playback_stop_before_the_end_only_resets_inactivity(series):
    normalized = normalize_event(_template("PlaybackStop", PlayedToCompletion="False"))
    assert normalized["event_type"] == "playback_stopped"
    assert normalized["played"] is False
    ingest_event(_template("PlaybackStop", PlayedToCompletion="False"))
    process_pending_events()
    assert _searches() == []
    assert db.session.query(UserMediaState).one().last_played_at is not None


def test_marking_watched_in_jellyfin_counts_as_played(series):
    payload = _template("UserDataSaved", Played="True", SaveReason="TogglePlayed")
    assert normalize_event(payload)["event_type"] == "item_played"
    ingest_event(payload)
    process_pending_events()
    assert _searches() == [1, 2]


def test_favorite_toggle_is_recorded_without_playback(series):
    payload = _template(
        "UserDataSaved",
        ItemId=str(SERIES),
        ItemType="Series",
        Favorite="True",
        Played="False",
        SaveReason="UpdateUserRating",
    )
    normalized = normalize_event(payload)
    assert normalized["event_type"] == "favorite_changed"
    assert normalized["favorite"] is True
    ingest_event(payload)
    process_pending_events()
    state = db.session.query(UserMediaState).one()
    assert state.favorite is True and state.last_played_at is None


def test_send_all_properties_uses_native_json_types(series):
    normalized = normalize_event(
        {
            "NotificationType": "PlaybackStop",
            "ItemId": EPISODE.hex,
            "UserId": USER.hex,
            "SeasonNumber": 1,
            "EpisodeNumber": 1,
            "PlayedToCompletion": True,
            "PlaybackPositionTicks": 12345,
        }
    )
    assert normalized["event_type"] == "item_played"
    assert normalized["season_number"] == 1
    assert normalized["position_ticks"] == 12345


def test_movie_template_with_empty_episode_fields_is_valid(app):
    normalized = normalize_event(
        _template("PlaybackStart", ItemType="Movie", SeriesId="", SeasonNumber="")
    )
    assert normalized["series_external_id"] is None
    assert normalized["season_number"] is None
    assert normalized["event_type"] == "playback_started"


def test_webhook_accepts_plugin_body_without_json_content_type(series, client):
    set_setting("webhook_token", "plugin-token")
    body = json.dumps(_template("PlaybackStart"))
    response = client.post(
        "/api/v1/webhook/jellyfin",
        data=body,
        headers={"X-Curatarr-Token": "plugin-token", "Content-Type": "text/plain"},
    )
    assert response.status_code == 202
    assert (
        client.post(
            "/api/v1/webhook/jellyfin",
            data="not json",
            headers={"X-Curatarr-Token": "plugin-token"},
        ).status_code
        == 400
    )
    assert client.post("/api/v1/webhook/jellyfin", data=body).status_code == 403
