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
    assert "Use this picture" in page and "As its wide tile" in page
    assert 'data-preview="#poster-preview"' in page and "poster-preview.js" in page
    # Its poster (Jellyfin's own here) opens full screen.
    assert f'data-full="/posters/{media.id}"' in page and "lightbox.js" in page
    custom_posters.apply(media, _picture("#cc2222"), "Uploaded")
    page = client.get(f"/titles/{media.id}").text
    assert "Restore original" in page
    poster = custom_posters.active(media.id)
    assert f'data-full="/library/posters/{poster.id}' in page  # and the custom one
    gallery = client.get("/library/posters").text
    assert "1 title with custom pictures" in gallery and media.title in gallery


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
    db.session.add_all(
        MediaPart(media_identity_id=m.id, kind="movie_file", has_file=True)
        for m in others
    )
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
            "wide": True,
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
    assert (
        "1 title with custom pictures" in page and "(3 posters across accounts)" in page
    )
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


class RibbonJellyfin(FakeJellyfin):
    poster_tag = "tag-1"

    def users(self):
        return [{"Id": u, "PrimaryImageTag": None} for u in ("u1", "u2", "u3")]

    def item_details(self, ids):
        return [{"Id": i, "ImageTags": {"Primary": self.poster_tag}} for i in ids]


def _favourite(media, user_id):
    from curatarr.models import UserMediaState

    db.session.add(
        UserMediaState(
            media_identity_id=media.id, jellyfin_user_id=user_id, favorite=True
        )
    )
    db.session.commit()


def test_favourite_ribbons_show_whose_favourite_a_title_is(app, client, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    jellyfin = RibbonJellyfin(_picture("#223344", size=(400, 600)))
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    accounts = {"u1": "Sam", "u2": "Alex", "u3": "Kim"}
    set_setting("jellyfin_user_names", accounts)
    set_setting("webhook_token", "secret")
    db.session.commit()
    _favourite(media, "u1")
    assert custom_posters.plugin_list() == []  # off by default
    # Alex sees Sam's favourites; Kim sees nobody's; Sam doesn't see their own.
    response = client.post(
        "/library/posters/ribbons", data={"enabled": "on", "see-u2-u1": "on"}
    )
    assert response.status_code == 302
    entries = custom_posters.plugin_list()
    assert [(e["user"], e["id"]) for e in entries] == [("u2", f"v.u2.{media.id}")]
    assert custom_posters.ribbons() == {
        ("t", "u2", media.id): {
            "users": ["u1"],
            "names": ["Sam"],
            "new": False,
            "household": False,
        }
    }
    picture = client.get(
        f"/api/v1/jellyfin/posters/{entries[0]['id']}.jpg",
        headers={"X-Curatarr-Token": "secret"},
    )
    assert picture.status_code == 200
    shown = Image.open(BytesIO(picture.data)).convert("RGB")
    gold = shown.getpixel((5, shown.height - 5))
    assert gold[0] > 200 and gold[1] > 150 and gold[2] < 90  # along the bottom, gold
    # A new poster in Jellyfin (Leaving Soon's banner, say) means a new tag.
    first = entries[0]["tag"]
    jellyfin.poster_tag = "tag-2"
    assert custom_posters.plugin_list()[0]["tag"] != first
    # Seeing their own too, and with two names it says "+ 1".
    _favourite(media, "u2")
    client.post(
        "/library/posters/ribbons",
        data={"enabled": "on", "own": "on", "see-u2-u1": "on", "position": "top_right"},
    )
    assert custom_posters.ribbons()[("t", "u2", media.id)]["names"] == ["Alex", "Sam"]
    assert custom_posters.ribbon_text(["Alex", "Sam"], "names") == "★ Alex + 1"
    assert ("t", "u1", media.id) in custom_posters.ribbons()  # Sam's own
    # Off: gone.
    client.post("/library/posters/ribbons", data={})
    assert custom_posters.plugin_list() == []


def test_a_ribbon_goes_on_an_accounts_own_poster(app, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    monkeypatch.setattr(
        "curatarr.services.client",
        lambda _kind: RibbonJellyfin(_picture("#223344", size=(400, 600))),
    )
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex"})
    db.session.commit()
    mine = custom_posters.apply(
        media, _picture("#22aa22", size=(400, 600)), "Uploaded", user_id="u2"
    )
    custom_posters.save_ribbon_settings(
        {"enabled": "on", "see-u2-u1": "on"}, [("u1", "Sam"), ("u2", "Alex")]
    )
    _favourite(media, "u1")
    entries = custom_posters.plugin_list()
    assert len(entries) == 1 and entries[0]["id"].startswith("v.")  # one, combined
    picture = Image.open(BytesIO(custom_posters.plugin_picture(entries[0]["id"])))
    assert picture.convert("RGB").getpixel((20, 300))[1] > 150  # Alex's green poster
    # Without the ribbon, Alex's own poster as before.
    custom_posters.save_ribbon_settings({}, [("u1", "Sam"), ("u2", "Alex")])
    assert custom_posters.plugin_list()[0]["id"] == mine.id


def _band_height(picture: bytes) -> int:
    """How far up from the bottom the ribbon reaches (stops at the poster's blue)."""
    shown = Image.open(BytesIO(picture)).convert("RGB")
    rows = 0
    while rows < shown.height:
        red, _green, blue = shown.getpixel((2, shown.height - 1 - rows))
        if blue > red:
            break
        rows += 1
    return rows


def test_a_ribbon_lists_names_where_its_shown_big(app, client, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    monkeypatch.setattr(
        "curatarr.services.client",
        lambda _kind: RibbonJellyfin(_picture("#223399", size=(400, 600))),
    )
    accounts = [("u1", "Sam"), ("u2", "Alex"), ("u3", "Kim")]
    set_setting("jellyfin_user_names", dict(accounts))
    set_setting("webhook_token", "secret")
    db.session.commit()
    _favourite(media, "u1")
    _favourite(media, "u3")
    custom_posters.save_ribbon_settings(
        {"enabled": "on", "see-u2-u1": "on", "see-u2-u3": "on"}, accounts
    )
    options = custom_posters.ribbon_settings()
    assert (options["list_style"], options["page_style"], options["colour"]) == (
        "faces",
        "names",
        "gold",
    )
    entry = custom_posters.plugin_list()[0]
    assert entry["large"]
    small = client.get(
        f"/api/v1/jellyfin/posters/{entry['id']}.jpg",
        headers={"X-Curatarr-Token": "secret"},
    ).data
    large = client.get(
        f"/api/v1/jellyfin/posters/{entry['id']}.jpg?size=large",
        headers={"X-Curatarr-Token": "secret"},
    ).data
    assert _band_height(large) > 1.5 * _band_height(small)  # two names, stacked
    # Rows of wide tiles get it landscape: the poster, ribbon and all, centred.
    assert entry["wide"]
    wide = Image.open(
        BytesIO(
            client.get(
                f"/api/v1/jellyfin/posters/{entry['id']}.jpg?size=wide",
                headers={"X-Curatarr-Token": "secret"},
            ).data
        )
    )
    assert wide.size == (1280, 720)
    middle = wide.convert("RGB").getpixel((640, 360))
    assert middle[2] > middle[0]  # the blue poster, at full brightness
    edge = wide.convert("RGB").getpixel((5, 360))
    assert max(edge) < max(middle)  # a darkened copy behind it
    # "As in lists" on a title's page: one picture, and a new look means new tags.
    custom_posters.save_ribbon_settings(
        {
            "enabled": "on",
            "see-u2-u1": "on",
            "see-u2-u3": "on",
            "page_style": "list",
            "colour": "teal",
        },
        accounts,
    )
    changed = custom_posters.plugin_list()[0]
    assert not changed["large"] and changed["tag"] != entry["tag"]
    teal = Image.open(BytesIO(custom_posters.plugin_picture(changed["id"]))).convert(
        "RGB"
    )
    assert (
        teal.getpixel((2, teal.height - 3))[1] > teal.getpixel((2, teal.height - 3))[0]
    )


def test_ribbon_settings_from_before_the_looks_carry_over(app):
    from curatarr.services import set_setting

    set_setting("favourite_ribbons", {"enabled": True, "label": "star"})
    db.session.commit()
    options = custom_posters.ribbon_settings()
    assert (options["list_style"], options["page_style"]) == ("star", "list")
    set_setting("favourite_ribbons", {"label": "names", "avatars": False})
    db.session.commit()
    assert custom_posters.ribbon_settings()["list_style"] == "names"


def test_the_ribbon_settings_page(app, client):
    from curatarr.services import set_setting

    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex"})
    db.session.commit()
    page = client.get("/library/posters").text
    assert "Favourite ribbons" in page and 'name="see-u1-u2"' in page
    assert 'name="see-u1-u1"' not in page  # nobody's set to see themselves here
    preview = client.get("/library/posters/ribbon-preview.jpg")
    assert preview.status_code == 200 and preview.mimetype == "image/jpeg"
    assert 'name="list_style"' in page and 'name="colour"' in page
    assert "size=large" in page  # the page's version, previewed
    big = client.get("/library/posters/ribbon-preview.jpg?size=large")
    assert big.status_code == 200 and big.data != preview.data


def test_everyone_sees_one_accounts_favourites_in_one_tick(app, client):
    from curatarr.services import set_setting

    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex", "u3": "Kim"})
    db.session.commit()
    client.post("/library/posters/ribbons", data={"enabled": "on", "all-u1": "on"})
    sees = custom_posters.ribbon_settings()["sees"]
    assert sees == {"u1": [], "u2": ["u1"], "u3": ["u1"]}
    page = client.get("/library/posters").text
    assert 'id="all-u1" name="all-u1" data-ribbon-all="u1" checked' in page
    assert '<details class="mb-2">' in page  # all or nothing: the grid stays shut
    # Only one account sees Alex's: mixed, so the grid opens.
    client.post(
        "/library/posters/ribbons",
        data={"enabled": "on", "all-u1": "on", "see-u3-u2": "on"},
    )
    page = client.get("/library/posters").text
    assert '<details class="mb-2" open>' in page
    assert "ribbon-grid.js" in page


def test_new_household_and_episode_ribbons(app, client, monkeypatch):
    from datetime import timedelta

    from curatarr.models import EpisodeUserState, MediaPart, UserMediaState, utcnow
    from curatarr.services import set_setting

    library, film, _part = _movie()
    jellyfin = RibbonJellyfin(_picture("#223344", size=(400, 600)))
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    accounts = [("u1", "Sam"), ("u2", "Alex"), ("u3", "Kim")]
    set_setting("jellyfin_user_names", dict(accounts))
    set_setting("webhook_token", "secret")
    db.session.commit()
    form = {
        "enabled": "on",
        "all-u1": "on",
        "new": "on",
        "new_days": "7",
        "household": "on",
        "household_at": "3",
        "episodes": "on",
    }
    custom_posters.save_ribbon_settings(form, accounts)
    # Sam favourited the film yesterday: NEW for Alex and Kim.
    db.session.add(
        UserMediaState(
            media_identity_id=film.id,
            jellyfin_user_id="u1",
            favorite=True,
            favorite_since=utcnow() - timedelta(days=1),
        )
    )
    db.session.commit()
    ribbon = custom_posters.ribbons()[("t", "u2", film.id)]
    assert ribbon["new"] and not ribbon["household"]
    # A month on, it isn't.
    state = db.session.query(UserMediaState).one()
    state.favorite_since = utcnow() - timedelta(days=30)
    db.session.commit()
    assert not custom_posters.ribbons()[("t", "u2", film.id)]["new"]
    # Everyone's favourite: a household favourite, shown to all three.
    for user in ("u2", "u3"):
        db.session.add(
            UserMediaState(
                media_identity_id=film.id, jellyfin_user_id=user, favorite=True
            )
        )
    db.session.commit()
    marked = custom_posters.ribbons()
    assert all(marked[("t", u, film.id)]["household"] for u in ("u2", "u3"))
    assert ("t", "u1", film.id) not in marked  # Sam's account isn't set up for ribbons
    assert custom_posters.ribbon_text(["a"], "names", True) == "♥ Household favourite"
    # A favourite episode's thumbnail.
    show = MediaIdentity(
        library_id=library.id, media_type="series", title="Show", jellyfin_id="show"
    )
    db.session.add(show)
    db.session.flush()
    episode = MediaPart(
        media_identity_id=show.id,
        kind="episode",
        season_number=1,
        episode_number=2,
        has_file=True,
        jellyfin_id="episode-2",
    )
    db.session.add(episode)
    db.session.flush()
    db.session.add(
        EpisodeUserState(media_part_id=episode.id, jellyfin_user_id="u1", favorite=True)
    )
    db.session.commit()
    entries = {e["id"]: e for e in custom_posters.plugin_list()}
    assert (
        f"e.u2.{episode.id}" in entries
        and entries[f"e.u2.{episode.id}"]["item"] == "episode2"
    )
    jellyfin.current = _picture("#335566", size=(1280, 720))
    picture = Image.open(BytesIO(custom_posters.plugin_picture(f"e.u2.{episode.id}")))
    assert picture.width > picture.height  # still a thumbnail
    assert not entries[f"e.u2.{episode.id}"]["large"]  # thumbnails: one size
    assert not entries[f"e.u2.{episode.id}"]["wide"]  # and wide already
    # Episodes switched off: gone.
    custom_posters.save_ribbon_settings({**form, "episodes": ""}, accounts)
    assert f"e.u2.{episode.id}" not in {e["id"] for e in custom_posters.plugin_list()}


def test_an_uploaded_picture_goes_on_the_ribbon(app, client, monkeypatch):
    from curatarr.services import set_setting

    set_setting("jellyfin_user_names", {"u1": "Sam"})
    db.session.commit()
    assert custom_posters.avatar_picture("u1") is None  # initials
    response = client.post(
        "/library/posters/avatar",
        data={"account": "u1", "picture": (BytesIO(_picture("#cc2222")), "me.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    face = Image.open(BytesIO(custom_posters.avatar_picture("u1")))
    assert face.size == (256, 256)
    assert client.get("/library/posters/avatar/u1.jpg").mimetype == "image/png"
    client.post("/library/posters/avatar", data={"account": "u1", "remove": "1"})
    assert custom_posters.avatar_picture("u1") is None
    assert (
        client.post("/library/posters/avatar", data={"account": "x"}).status_code == 400
    )


def test_a_ribbon_along_the_top_sits_below_leaving_soon(app):
    from curatarr.artwork import badge_poster, favourite_ribbon

    poster = _picture("#223399", size=(400, 600))
    shown = Image.open(
        BytesIO(favourite_ribbon(poster, "★", "top", avatars=[(None, "Sam")]))
    ).convert("RGB")
    assert shown.getpixel((3, 3))[2] < 90  # gold at the very top
    assert shown.getpixel((3, shown.height - 3))[2] > 120  # the poster at the bottom
    leaving = badge_poster(poster, "7 DAYS LEFT")
    shown = Image.open(
        BytesIO(
            favourite_ribbon(
                leaving, "★", "top", avatars=[(None, "Sam")], leaving="7 DAYS LEFT"
            )
        )
    ).convert("RGB")
    red = shown.getpixel((3, round(shown.height * 0.06)))
    assert red[0] > 200 and red[1] < 60  # Leaving Soon's tag still shows


def test_ribbon_previews_use_a_real_poster(app, client, monkeypatch):
    from curatarr import custom_posters as module

    _library, media, _part = _movie()
    monkeypatch.setattr(
        "curatarr.services.client",
        lambda _kind: RibbonJellyfin(_picture("#22aa22", size=(400, 600))),
    )
    module._preview.clear()
    _favourite(media, "u1")
    preview = client.get("/library/posters/ribbon-preview.jpg")
    shown = Image.open(BytesIO(preview.data)).convert("RGB")
    assert shown.getpixel((shown.width // 2, shown.height // 3))[1] > 150  # its green


def test_the_gallery_sorts_as_chosen_and_remembers_it(app, client, monkeypatch):
    from datetime import timedelta

    from curatarr.models import utcnow

    library, first, _part = _movie()
    _fake(monkeypatch, _picture("#223344"))
    second = MediaIdentity(
        library_id=library.id, media_type="movie", title="Zebra", jellyfin_id="z"
    )
    third = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title="The Apple",
        sort_title="Apple",
        jellyfin_id="a",
    )
    db.session.add_all([second, third])
    db.session.commit()
    for days, media in ((3, first), (2, second), (1, third)):
        poster = custom_posters.apply(media, _picture("#cc2222"), "Uploaded")
        poster.applied_at = utcnow() - timedelta(days=days)
    db.session.commit()

    def order(sort):
        return [c["media"].title for c in custom_posters.showcase(sort)]

    assert order("newest") == ["The Apple", "Zebra", "Film"]
    assert order("oldest") == ["Film", "Zebra", "The Apple"]
    assert order("az") == ["The Apple", "Film", "Zebra"]  # by its sort title
    assert order("za") == ["Zebra", "Film", "The Apple"]

    def shown(url):
        page = client.get(url).text
        return [page.index(t) for t in ("Zebra", "The Apple")]

    zebra, apple = shown("/library/posters/gallery?sort=za")
    assert zebra < apple
    zebra, apple = shown("/library/posters/gallery")  # remembered
    assert zebra < apple
    zebra, apple = shown("/library/posters")  # and on the Posters page
    assert zebra < apple
    zebra, apple = shown("/library/posters?sort=bogus")  # ignored
    assert zebra < apple
    assert 'aria-current="true">Title Z–A' in client.get("/library/posters").text
