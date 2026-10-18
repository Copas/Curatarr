"""A star with who favourited a title in Jellyfin, wherever it's listed."""

from test_request_removal import _film

from curatarr import db
from curatarr.models import UserMediaState
from curatarr.services import favorited_by, set_setting


def _favourite(media, user_id):
    db.session.add(
        UserMediaState(
            media_identity_id=media.id, jellyfin_user_id=user_id, favorite=True
        )
    )
    db.session.commit()


def test_who_favourited_shows_on_every_page(app, client):
    with app.app_context():
        media = _film()
        _favourite(media, "kid")
        set_setting("jellyfin_user_names", {"viewer": "Us", "kid": "Kid"})
        db.session.commit()
        media_id = media.id
        assert favorited_by([media_id]) == {media_id: ["Kid"]}
    assert "Favorited by Kid" in client.get(f"/titles/{media_id}").text
    titles = client.get("/titles").text
    assert 'class="fav-star"' in titles and "Kid</span>" in titles
    assert 'class="fav-star"' in client.get("/free-space?watched=any").text


def test_no_star_without_a_favourite(app, client):
    with app.app_context():
        media_id = _film().id
        assert favorited_by([media_id]) == {}
    assert "Favorited by" not in client.get(f"/titles/{media_id}").text
    assert 'class="fav-star"' not in client.get("/titles").text
