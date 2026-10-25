"""Custom posters: put on, restored, imported from a folder."""

import os
import time
from io import BytesIO

from PIL import Image
from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import custom_posters, db
from curatarr.models import CustomPoster, LifecycleAction, MediaIdentity, MediaPart


def _picture(colour, size=(300, 450), kind="JPEG"):
    output = BytesIO()
    Image.new("RGB", size, colour).save(output, format=kind)
    return output.getvalue()


class FakeJellyfin:
    def __init__(self, current):
        self.current, self.uploads, self.downloaded = current, 0, []

    def image(self, _item_id, _kind="Primary"):
        return self.current

    def put_image(self, _item_id, data, _kind="Primary"):
        self.current, self.uploads = data, self.uploads + 1

    def remote_images(self, _item_id, _kind="Primary"):
        return [{"Url": "https://provider/poster.jpg", "ProviderName": "TMDb"}]

    def download_remote_image(self, _item_id, image, _kind="Primary"):
        self.downloaded.append(image["Url"])


def _fake(monkeypatch, current):
    jellyfin = FakeJellyfin(current)
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    return jellyfin


def test_a_poster_goes_on_and_the_original_comes_back(app, monkeypatch):
    _library, media, _part = _movie()
    original = _picture("#223344")
    jellyfin = _fake(monkeypatch, original)
    record = custom_posters.apply(media, _picture("#cc2222", kind="PNG"), "Uploaded")
    assert jellyfin.uploads == 1 and record.original_path
    # A second one replaces it, keeping the very first original.
    second = custom_posters.apply(media, _picture("#22cc22"), "Uploaded")
    assert second.original_path == record.original_path
    assert db.session.query(CustomPoster).filter_by(active=True).count() == 1
    custom_posters.restore(second)
    assert custom_posters._same_picture(jellyfin.current, original)
    assert custom_posters.active(media.id) is None
    kinds = {a.action_type for a in db.session.query(LifecycleAction)}
    assert {"custom_poster_applied", "custom_poster_restored"} <= kinds


def test_a_picture_already_showing_is_adopted_and_restores_from_the_provider(
    app, monkeypatch
):
    _library, media, _part = _movie()
    mine = _picture("#cc2222")
    jellyfin = _fake(monkeypatch, mine)
    record = custom_posters.apply(media, mine, "08_film.jpg", adopt=True)
    assert jellyfin.uploads == 0 and record.original_path is None
    assert custom_posters.apply(media, mine, "08_film.jpg", adopt=True) is None
    custom_posters.restore(record)
    assert jellyfin.downloaded == ["https://provider/poster.jpg"]


def test_not_a_picture_is_refused(app, monkeypatch):
    _library, media, _part = _movie()
    _fake(monkeypatch, _picture("#223344"))
    try:
        custom_posters.apply(media, b"not an image", "Uploaded")
    except ValueError as exc:
        assert "isn't a picture" in str(exc)
    else:
        raise AssertionError("accepted a file that isn't a picture")


def test_file_names_find_their_title(app):
    assert custom_posters.parse_name("08_the-big-lebowski.jpg") == (
        "the big lebowski",
        None,
    )
    assert custom_posters.parse_name("48_the-grudge-2004.jpg") == ("the grudge", 2004)
    assert custom_posters.parse_name("The Grudge (2019).png") == ("the grudge", 2019)
    assert custom_posters.parse_name("2012.jpg") == (
        "2012",
        None,
    )  # a title, not a year
    library, film, _part = _movie("/movies/The Grudge (2004)/Grudge.mkv")
    film.title = "The Grudge"
    other = MediaIdentity(
        library_id=library.id, media_type="movie", title="The Grudge", jellyfin_id="g2"
    )
    birdman = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title="Birdman or (The Unexpected Virtue of Ignorance)",
        jellyfin_id="b1",
    )
    db.session.add_all([other, birdman])
    db.session.flush()
    db.session.add(
        MediaPart(
            media_identity_id=other.id,
            kind="movie_file",
            has_file=True,
            file_path="/movies/The Grudge (2019)/Grudge.mkv",
        )
    )
    db.session.commit()
    titles = custom_posters._titles()
    assert custom_posters.match("48_the-grudge-2004.jpg", titles)[0] == film
    assert custom_posters.match("The Grudge (2019).jpg", titles)[0] == other
    assert custom_posters.match("the-grudge.jpg", titles)[0] is None  # which one?
    assert custom_posters.match("09_birdman.jpg", titles)[0] == birdman
    assert custom_posters.match("nothing-like-it.jpg", titles)[0] is None
    assert custom_posters.match("Birdman (2015).jpg", titles)[0] == birdman
    assert custom_posters.match("48_the-grudge-2005.jpg", titles)[0] == film
    assert custom_posters.match("48_the-grudge-1990.jpg", titles)[0] is None


def test_the_import_folder_puts_new_pictures_on(
    app,
    media_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    _library, media, _part = _movie("/movies/Film (2020)/Film.mkv")
    jellyfin = _fake(monkeypatch, _picture("#223344"))
    custom_posters.save_settings(str(tmp_path))
    settled = time.time() - 60
    for name, colour in (("01_film.jpg", "#cc2222"), ("02_unknown.jpg", "#22cc22")):
        (tmp_path / name).write_bytes(_picture(colour))
        os.utime(tmp_path / name, (settled, settled))
    (tmp_path / "notes.md").write_text("not a picture")
    assert custom_posters.sweep() == 1
    assert (
        jellyfin.uploads == 1
        and custom_posters.active(media.id).source == "01_film.jpg"
    )
    assert "02_unknown.jpg" in custom_posters.import_state()["unmatched"]
    assert custom_posters.sweep() == 0  # looked at again only every few minutes


def test_the_pages(app, client, monkeypatch):
    _library, media, _part = _movie()
    _fake(monkeypatch, _picture("#223344"))
    page = client.get(f"/titles/{media.id}").text
    assert "Use this poster" in page
    assert 'data-preview="#poster-preview"' in page and "poster-preview.js" in page
    custom_posters.apply(media, _picture("#cc2222"), "Uploaded")
    assert "Restore original" in client.get(f"/titles/{media.id}").text
    gallery = client.get("/library/posters").text
    assert "1 custom poster" in gallery and media.title in gallery


def test_uploading_a_big_picture_on_the_title_page(app, client, monkeypatch):
    """Requests are limited to 1 MB elsewhere; a poster is often more."""
    _library, media, _part = _movie()
    jellyfin = _fake(monkeypatch, _picture("#223344"))
    noisy = Image.frombytes("RGB", (1024, 1536), os.urandom(1024 * 1536 * 3))
    output = BytesIO()
    noisy.save(output, format="PNG")
    assert len(output.getvalue()) > 2 * 1024 * 1024
    response = client.post(
        f"/titles/{media.id}/poster",
        data={"poster": (BytesIO(output.getvalue()), "big.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302 and jellyfin.uploads == 1
    assert client.post(f"/titles/{media.id}/poster/restore").status_code == 302
    assert custom_posters.active(media.id) is None


def test_prompts_for_an_ai_in_batches(app, client, monkeypatch, tmp_path):
    from curatarr.models import UserMediaState

    library, film, _part = _movie("/movies/The Big Lebowski (1998)/Film.mkv")
    film.title = "The Big Lebowski"
    others = [
        MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Film {n}",
            jellyfin_id=f"j{n}",
        )
        for n in range(3)
    ]
    db.session.add_all(others)
    db.session.flush()
    for media in [film, *others[:2]]:  # favourites: three of the four
        db.session.add(
            UserMediaState(
                media_identity_id=media.id, jellyfin_user_id="u1", favorite=True
            )
        )
    db.session.commit()

    class Details(FakeJellyfin):
        def item_details(self, ids):
            return [
                {
                    "Id": "jf-film",
                    "ProductionYear": 1998,
                    "Genres": ["Comedy", "Crime"],
                    "Overview": "The Dude is mistaken for a millionaire.",
                }
            ]

    jellyfin = Details(_picture("#223344"))
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    custom_posters.apply(others[0], _picture("#cc2222"), "Uploaded")  # done already
    titles = custom_posters.prompt_titles("favourites:u1")
    assert [m.title for m in titles] == ["Film 1", "The Big Lebowski"]
    described = custom_posters.describe(titles)
    assert (
        described[1]["file"] == "02_the-big-lebowski-1998.jpg"
    )  # A to Z: Film 1 first
    texts = custom_posters.prompts(
        described, batch=1, photos="/photos", people="a couple", output="/posters"
    )
    assert len(texts) == 2 and "Batch 2 of 2" in texts[1]
    assert "/photos" in texts[1] and "a couple" in texts[1]
    assert "→ 02_the-big-lebowski-1998.jpg" in texts[1] and "/posters" in texts[1]
    assert "mistaken for a millionaire" in texts[1]
    # The file names the prompts ask for are ones the import folder matches.
    assert (
        custom_posters.match(described[1]["file"], custom_posters._titles())[0] == film
    )
    # And the page.
    custom_posters.save_settings(str(tmp_path))
    client.post(
        "/library/posters/prompts",
        data={"titles": "favourites:u1", "batch": "20", "skip_done": "on"},
    )
    page = client.get("/library/posters?prompts=1").text
    assert "Batch 1" in page and "02_the-big-lebowski-1998.jpg" in page


def test_a_poster_for_one_account_and_the_plugins_list(app, client, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    jellyfin = _fake(monkeypatch, _picture("#223344"))
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex"})
    set_setting("webhook_token", "secret")
    db.session.commit()
    response = client.post(
        f"/titles/{media.id}/poster",
        data={"poster": (BytesIO(_picture("#cc2222")), "mine.jpg"), "for": "u1"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert jellyfin.uploads == 0  # Jellyfin's own poster stays for everyone else
    mine = custom_posters.active(media.id, "u1")
    assert mine and custom_posters.active(media.id) is None
    assert "Sam" in client.get(f"/titles/{media.id}").text
    # The plugin's list and picture, with the webhook token only.
    assert client.get("/api/v1/jellyfin/posters").status_code == 403
    listing = client.get(
        "/api/v1/jellyfin/posters", headers={"X-Curatarr-Token": "secret"}
    ).get_json()
    assert listing["posters"] == [
        {
            "item": "jffilm",
            "user": "u1",
            "id": mine.id,
            "tag": mine.id.replace("-", ""),
        }
    ]
    picture = client.get(
        f"/api/v1/jellyfin/posters/{mine.id}.jpg",
        headers={"X-Curatarr-Token": "secret"},
    )
    assert picture.status_code == 200 and picture.mimetype == "image/jpeg"
    # Another account: refused unless Jellyfin knows it.
    assert (
        client.post(
            f"/titles/{media.id}/poster",
            data={"poster": (BytesIO(_picture("#cc2222")), "x.jpg"), "for": "nobody"},
            content_type="multipart/form-data",
        ).status_code
        == 400
    )
    # Removing it shows the usual poster again, and leaves Jellyfin's alone.
    client.post(f"/titles/{media.id}/poster/restore", data={"poster": mine.id})
    assert custom_posters.active(media.id, "u1") is None and jellyfin.downloaded == []
    assert (
        client.get(
            "/api/v1/jellyfin/posters", headers={"X-Curatarr-Token": "secret"}
        ).get_json()["posters"]
        == []
    )


def test_imported_posters_for_chosen_accounts_give_everyone_else_the_usual_one(
    app,
    media_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from curatarr.services import set_setting

    _library, media, _part = _movie("/movies/Film (2020)/Film.mkv")
    mine = _picture("#cc2222")
    # Put on for everyone before (by hand, unrecorded), so Jellyfin shows it now.
    jellyfin = _fake(monkeypatch, mine)
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex", "u3": "Kim"})
    custom_posters.save_settings(str(tmp_path), ["u1", "u2"])
    settled = time.time() - 60
    (tmp_path / "01_film.jpg").write_bytes(mine)
    os.utime(tmp_path / "01_film.jpg", (settled, settled))
    assert custom_posters.sweep() == 1
    assert custom_posters.active(media.id, "u1") and custom_posters.active(
        media.id, "u2"
    )
    assert custom_posters.active(media.id, "u3") is None
    assert jellyfin.downloaded == ["https://provider/poster.jpg"]  # everyone else
    # Looked at again (who it's for changed): nothing more to do.
    custom_posters.save_settings(str(tmp_path), ["u1"])
    jellyfin.current = _picture("#223344")  # the provider's poster is on now
    assert custom_posters.sweep() == 0
    assert len(jellyfin.downloaded) == 1


def test_a_recorded_poster_for_everyone_becomes_one_for_chosen_accounts(
    app,
    media_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from curatarr.services import set_setting

    _library, media, _part = _movie("/movies/Film (2020)/Film.mkv")
    original = _picture("#223344")
    jellyfin = _fake(monkeypatch, original)
    mine = _picture("#cc2222")
    custom_posters.apply(media, mine, "01_film.jpg")  # everyone's, original kept
    set_setting("jellyfin_user_names", {"u1": "Sam"})
    custom_posters.save_settings(str(tmp_path), ["u1"])
    settled = time.time() - 60
    (tmp_path / "01_film.jpg").write_bytes(mine)
    os.utime(tmp_path / "01_film.jpg", (settled, settled))
    assert custom_posters.sweep() == 1
    assert (
        custom_posters.active(media.id, "u1")
        and custom_posters.active(media.id) is None
    )
    assert custom_posters._same_picture(jellyfin.current, original)  # the real original


def test_the_gallery_shows_one_tile_per_picture(app, client, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    _fake(monkeypatch, _picture("#223344"))
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex", "u3": "Kim"})
    db.session.commit()
    mine = _picture("#cc2222")
    for user in ("u1", "u2", "u3"):
        custom_posters.apply(media, mine, "01_film.jpg", user_id=user)
    groups = custom_posters.gallery_groups()
    assert len(groups) == 1 and len(groups[0]["posters"]) == 3
    page = client.get("/library/posters").text
    assert "1 custom poster" in page and "(3 across accounts)" in page
    assert page.count("Remove for all") == 1
    # Remove for all: every account's goes in one go.
    client.post(
        f"/titles/{media.id}/poster/restore",
        data={"back": "gallery", "poster": [p.id for p in groups[0]["posters"]]},
    )
    assert custom_posters.gallery() == []


def test_the_page_title_is_only_words(app, client):
    import re

    page = client.get("/library/posters").text
    title = re.search(r"<title>(.*?)</title>", page, re.DOTALL).group(1)
    assert "<" not in title and "Posters" in title


def test_an_accounts_poster_carries_the_leaving_soon_banner(app, client, monkeypatch):
    """Everyone else's poster shows the countdown; the account's own must too."""
    from datetime import timedelta

    from curatarr.models import PosterSnapshot, PurgeCandidate, utcnow
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    _fake(monkeypatch, _picture("#223344"))
    set_setting("jellyfin_user_names", {"u1": "Sam"})
    set_setting("webhook_token", "secret")
    db.session.commit()
    mine = custom_posters.apply(media, _picture("#22aa22"), "Uploaded", user_id="u1")
    token = {"X-Curatarr-Token": "secret"}

    def listed():
        return client.get("/api/v1/jellyfin/posters", headers=token).get_json()[
            "posters"
        ][0]["tag"]

    def served():
        return client.get(f"/api/v1/jellyfin/posters/{mine.id}.jpg", headers=token).data

    plain_tag, plain_picture = listed(), served()
    assert plain_tag == mine.id.replace("-", "")
    candidate = PurgeCandidate(
        media_identity_id=media.id,
        state="LEAVING_SOON",
        reason_code="inactivity",
        reason_text="Old",
        reclaimable_bytes=1,
        scheduled_delete_at=utcnow() + timedelta(days=7),
    )
    db.session.add(candidate)
    db.session.flush()
    db.session.add(
        PosterSnapshot(
            media_identity_id=media.id,
            jellyfin_item_id=media.jellyfin_id,
            candidate_id=candidate.id,
            image_type="Primary",
            active=True,
        )
    )
    db.session.commit()
    leaving_tag, leaving_picture = listed(), served()
    assert leaving_tag != plain_tag  # apps fetch it again
    assert leaving_picture != plain_picture
    # The red tag is in the top-left corner.
    corner = Image.open(BytesIO(leaving_picture)).convert("RGB").getpixel((30, 40))
    assert corner[0] > 180 and corner[1] < 80
    # The banner's words change day by day, and so does the tag.
    candidate.scheduled_delete_at = utcnow() + timedelta(days=3)
    db.session.commit()
    assert listed() not in {plain_tag, leaving_tag}
    # Kept: the plain poster again.
    candidate.state = "KEPT"
    db.session.commit()
    assert listed() == plain_tag
