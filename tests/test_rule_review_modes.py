from datetime import timedelta

import pytest

from curatarr import db
from curatarr.lifecycle import evaluate_retention
from curatarr.models import (
    Library,
    LibraryPolicy,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    TitleOverride,
    UserMediaState,
    utcnow,
)
from curatarr.policy import validate_policy


@pytest.fixture(autouse=True)
def _inactivity_cleanup_enabled(app):
    """These tests exercise inactivity cleanup, which libraries must opt into."""
    from curatarr.services import set_setting

    with app.app_context():
        set_setting("global_policy", {"inactivity_cleanup": True})


def _movie_library(policy, *, played_days_ago=None):
    library = Library(
        jellyfin_library_id="rule-modes", name="Rule Movies", media_type="movies"
    )
    db.session.add(library)
    db.session.flush()
    db.session.add(LibraryPolicy(library_id=library.id, policy_json=policy))
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title="Rule Film",
        jellyfin_id="rule-movie",
        radarr_id=1,
        tmdb_id=20001,
        added_at=utcnow() - timedelta(days=200),
    )
    db.session.add(media)
    db.session.flush()
    db.session.add(
        MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            size_bytes=100,
        )
    )
    if played_days_ago is not None:
        db.session.add(
            UserMediaState(
                media_identity_id=media.id,
                jellyfin_user_id="viewer",
                last_played_at=utcnow() - timedelta(days=played_days_ago),
            )
        )
    db.session.commit()
    return media


QUOTA = {"quota_enabled": True, "high_water_bytes": 50, "low_water_bytes": 10}


@pytest.mark.parametrize(
    ("policy", "played_days_ago", "reason", "state"),
    [
        ({"inactivity_review_mode": "automatic"}, None, "inactivity", "LEAVING_SOON"),
        ({"quota_review_mode": "automatic"}, None, "inactivity", "REVIEW"),
        (QUOTA | {"quota_review_mode": "automatic"}, 10, "quota", "LEAVING_SOON"),
        (QUOTA | {"inactivity_review_mode": "automatic"}, 10, "quota", "REVIEW"),
        (
            {"review_mode": "automatic", "inactivity_review_mode": "recommend"},
            None,
            "inactivity",
            "ELIGIBLE",
        ),
        ({}, None, "inactivity", "REVIEW"),
    ],
)
def test_rule_review_mode_applies_only_to_its_rule(
    app, policy, played_days_ago, reason, state
):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        _movie_library(policy, played_days_ago=played_days_ago)
        assert evaluate_retention() == 1
        candidate = db.session.query(PurgeCandidate).one()
        assert candidate.reason_code == reason
        assert candidate.state == state
        assert (candidate.scheduled_delete_at is not None) == (state == "LEAVING_SOON")


def test_title_review_mode_overrides_rule_mode(app):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        media = _movie_library({"inactivity_review_mode": "automatic"})
        db.session.add(
            TitleOverride(
                media_identity_id=media.id,
                override_json={"review_mode": "require_review"},
            )
        )
        db.session.commit()
        assert evaluate_retention() == 1
        assert db.session.query(PurgeCandidate).one().state == "REVIEW"


def test_rule_review_modes_are_validated():
    assert validate_policy({"quota_review_mode": "automatic"}, complete=False)
    with pytest.raises(ValueError):
        validate_policy({"inactivity_review_mode": "always"}, complete=False)


def test_library_policy_form_saves_rule_review_modes(app, client):
    with app.app_context():
        library = Library(
            jellyfin_library_id="form-lib", name="Form Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.commit()
        url = f"/libraries/{library.id}/policy"
        page = client.get(url)
        assert page.status_code == 200
        assert b"Quota cleanup review mode" in page.data
        client.post(
            url,
            data={"quota_review_mode": "automatic", "inactivity_review_mode": ""},
        )
        db.session.refresh(library)
        assert library.policy.policy_json == {"quota_review_mode": "automatic"}
