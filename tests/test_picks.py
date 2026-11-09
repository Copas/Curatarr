from datetime import timedelta

from curatarr import db, picks
from curatarr.models import Library, MediaIdentity, UserMediaState, utcnow
from curatarr.services import set_setting, setting


def test_picks_take_several_liked_films_to_agree_and_keep_some_range():
    vecs = picks.vectors(
        {
            "western1": {"g:western", "k:cowboy", "k:revenge"},
            "western2": {"g:western", "k:cowboy", "k:frontier"},
            "western3": {"g:western", "k:frontier", "k:revenge"},
            "noir": {"g:crime", "k:detective", "k:femme fatale"},
            "noir2": {"g:crime", "k:detective", "k:rain"},
            "cartoon": {"g:animation", "k:talking animal", "k:friendship"},
            "cartoon2": {"g:animation", "k:talking animal", "k:school"},
            "new_western": {"g:western", "k:cowboy", "k:revenge"},
            "new_noir": {"g:crime", "k:detective", "k:rain"},
            "new_cartoon": {"g:animation", "k:talking animal", "k:school"},
        }
    )
    liked = {"western1": 3, "western2": 1, "western3": 1, "noir": 1}
    candidates = {
        "new_western": ["western"],
        "new_noir": ["crime"],
        "new_cartoon": ["animation"],
    }
    chosen = picks.choose(liked, candidates, vecs)
    keys = [key for key, _score, _because in chosen]
    assert keys[0] == "new_western" and "new_noir" in keys
    assert "new_cartoon" not in keys  # nothing liked is like it
    # a pick left unplayed for weeks makes way
    faded = picks.choose(liked, candidates, vecs, shown={"new_western": 6})
    assert faded[0][0] == "new_noir"


def test_one_liked_film_or_genre_cant_fill_the_list(monkeypatch):
    monkeypatch.setattr(picks, "PER_LIKED", 2)
    films = {f"horror{n}": {"g:horror", "k:ghost", f"k:house{n % 2}"} for n in range(8)}
    films["loved"] = {"g:horror", "k:ghost", "k:house0", "k:house1"}
    for n in range(40):  # the rest of the library: other things entirely
        films[f"other{n}"] = {f"g:other{n % 5}", f"k:thing{n}", f"k:more{n % 7}"}
    vecs = picks.vectors(films)
    candidates = {f"horror{n}": ["horror"] for n in range(8)}
    monkeypatch.setattr(picks, "COUNT", 3)
    chosen = picks.choose({"loved": 3, "other1": 1}, candidates, vecs)
    # one liked film explains two at most; nothing else fits, so the best
    # of the rest fill the list
    assert len(chosen) == 3
    assert [because for _k, _s, because in chosen].count("loved") == 3


class FakeJellyfin:
    def __init__(self, users, unplayed):
        self.users_list, self.unplayed = users, unplayed
        self.playlists, self.played, self.deleted = {}, set(), []

    def users(self):
        return self.users_list

    def unplayed_movies(self, user_id):
        return [
            {"Id": i, "Genres": ["Western"], "CommunityRating": 7.5}
            for i in self.unplayed
            if i not in self.played
        ]

    def create_playlist(self, name, user_id, ids):
        key = f"pl-{len(self.playlists) + 1}"
        self.playlists[key] = {"name": name, "ids": list(ids)}
        return key

    def playlist_items(self, playlist_id, user_id):
        if playlist_id not in self.playlists:
            return None
        return [
            {"Id": i, "UserData": {"Played": i in self.played}}
            for i in self.playlists[playlist_id]["ids"]
        ]

    def set_playlist(self, playlist_id, ids):
        self.playlists[playlist_id]["ids"] = list(ids)

    def delete_item(self, item_id):
        self.deleted.append(item_id)
        self.playlists.pop(item_id, None)


def _library():
    library = Library(jellyfin_library_id="films", name="Films", media_type="movies")
    db.session.add(library)
    db.session.flush()
    for n in range(20):
        db.session.add(
            MediaIdentity(
                library_id=library.id,
                media_type="movie",
                title=f"Western {n}",
                jellyfin_id=f"jf{n:02}",
                genres=["western"],
                keywords=["cowboy", "frontier", f"town{n % 3}"],
            )
        )
    for n in range(60):  # the rest of the library: other things entirely
        db.session.add(
            MediaIdentity(
                library_id=library.id,
                media_type="movie",
                title=f"Other {n}",
                genres=[f"genre{n % 5}"],
                keywords=[f"thing{n}", f"more{n % 7}"],
            )
        )
    db.session.flush()
    films = db.session.query(MediaIdentity).order_by(MediaIdentity.title).all()
    by_jellyfin = {m.jellyfin_id: m for m in films}
    for n in range(6):  # Sam liked six; Pat only two
        db.session.add(
            UserMediaState(
                media_identity_id=by_jellyfin[f"jf{n:02}"].id,
                jellyfin_user_id="sam",
                play_count=1,
                favorite=n == 0,
            )
        )
    for n in range(2):
        db.session.add(
            UserMediaState(
                media_identity_id=by_jellyfin[f"jf{n:02}"].id,
                jellyfin_user_id="pat",
                play_count=1,
            )
        )
    set_setting("jellyfin_user_names", {"sam": "Sam", "pat": "Pat"})
    set_setting("curation", {"picks": True})
    db.session.commit()


def test_each_person_gets_a_playlist_kept_up_to_date(app, monkeypatch):
    _library()
    users = [{"Id": "sam", "Name": "Sam"}, {"Id": "pat", "Name": "Pat"}]
    jellyfin = FakeJellyfin(users, [f"jf{n:02}" for n in range(20)])
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    now = utcnow()
    assert picks.sweep(now) == 1  # Sam's; Pat has too little to go on
    (playlist,) = jellyfin.playlists.values()
    assert playlist["name"] == "Picks for Sam" and len(playlist["ids"]) == 10
    assert not {f"jf{n:02}" for n in range(6)} & set(playlist["ids"])  # liked
    assert setting("picks")["pat"]["liked"] == 2
    # watched: off the list at the next check, a few minutes later
    first = playlist["ids"][0]
    jellyfin.played.add(first)
    assert picks.sweep(now + timedelta(minutes=1)) == 0  # not yet
    picks.sweep(now + picks.CHECK_PLAYED)
    assert first not in playlist["ids"] and len(playlist["ids"]) == 9
    # a week on, made again
    assert picks.sweep(now + picks.REBUILD + picks.CHECK_PLAYED) == 1
    assert len(playlist["ids"]) == 10 and first not in playlist["ids"]


def test_picks_switched_off_or_an_account_left_out_removes_its_playlist(
    app, monkeypatch
):
    _library()
    users = [{"Id": "sam", "Name": "Sam"}]
    jellyfin = FakeJellyfin(users, [f"jf{n:02}" for n in range(20)])
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    now = utcnow()
    picks.sweep(now)
    assert jellyfin.playlists
    set_setting("curation", {"picks": True, "picks_skip": ["sam"]})
    db.session.commit()
    picks.sweep(now + picks.CHECK_PLAYED)
    assert jellyfin.deleted == ["pl-1"] and "sam" not in setting("picks")
    set_setting("curation", {"picks": True})
    db.session.commit()
    picks.sweep(now + 2 * picks.CHECK_PLAYED)
    assert len(jellyfin.playlists) == 1
    set_setting("curation", {"picks": False})
    db.session.commit()
    picks.sweep(now + 3 * picks.CHECK_PLAYED)
    assert not jellyfin.playlists and setting("picks") == {}


def test_nothing_is_asked_of_jellyfin_while_picks_are_off(app, monkeypatch):
    def no_client(_kind):
        raise AssertionError("asked Jellyfin")

    monkeypatch.setattr("curatarr.services.client", no_client)
    assert picks.sweep() == 0
