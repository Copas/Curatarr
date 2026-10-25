"""Accounts hidden from Jellyfin's sign-in screen: shown, left out of
Curatarr's pages, or ignored."""

from curatarr import db
from curatarr.models import Library, MediaIdentity, UserMediaState
from curatarr.services import (
    account_names,
    favorited_by,
    left_out_user_ids,
    reconcile_user_state,
    save_hidden_accounts,
    setting,
)


class Jellyfin:
    def plugins(self):
        return []

    def packages(self):
        return []

    def sessions(self):
        return []

    def users(self):
        return [
            {"Id": "sam", "Name": "Sam", "Policy": {}},
            {"Id": "pat", "Name": "Pat", "Policy": {"IsHidden": True}},
        ]

    def user_items(self, _user, _library, start, _limit):
        if start:
            return {"Items": [], "TotalRecordCount": 1}
        return {
            "Items": [
                {"Id": "film", "Type": "Movie", "UserData": {"IsFavorite": True}}
            ],
            "TotalRecordCount": 1,
        }


def _film():
    library = Library(jellyfin_library_id="films", name="Films", media_type="movies")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id, media_type="movie", title="A Film", jellyfin_id="film"
    )
    db.session.add(media)
    db.session.commit()
    return media


def _favourites():
    return sorted(
        u
        for (u,) in db.session.query(UserMediaState.jellyfin_user_id).filter_by(
            favorite=True
        )
    )


def test_hidden_accounts_shown_left_out_or_ignored(app, client, monkeypatch):
    monkeypatch.setattr("curatarr.services.client", lambda _kind: Jellyfin())
    with app.app_context():
        media = _film()
        reconcile_user_state()
        # Shown, by default, like any other account.
        assert setting("jellyfin_hidden_user_ids") == ["pat"]
        assert _favourites() == ["pat", "sam"]
        assert favorited_by([media.id]) == {media.id: ["Pat", "Sam"]}
        # Left out: never listed or named, but still read (and still counted).
        save_hidden_accounts("leave_out")
        assert left_out_user_ids() == {"pat"}
        assert account_names() == {"sam": "Sam"}
        assert favorited_by([media.id]) == {media.id: ["Sam"]}
        reconcile_user_state()
        assert _favourites() == ["pat", "sam"]
        # Ignored: what was read is forgotten and not read again.
        save_hidden_accounts("ignore")
        assert _favourites() == ["sam"]
        reconcile_user_state()
        assert _favourites() == ["sam"]
    page = client.get("/settings").text
    assert "Hidden Jellyfin accounts" in page and "(here: Pat)" in page
    client.post(
        "/settings", data={"kind": "hidden_accounts", "hidden_accounts": "show"}
    )
    with app.app_context():
        assert setting("hidden_accounts") == "show"
        reconcile_user_state()
        assert _favourites() == ["pat", "sam"]  # read again
