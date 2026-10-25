"""A title's other pictures: its wide tile, background, logo and banner, put
on and restored like posters, imported, asked for in prompts, and shown to
one account by the Jellyfin plugin."""

import os
import time
from io import BytesIO

from PIL import Image
from test_custom_posters import _favourite, _picture
from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import custom_posters, db
from curatarr.integrations import IntegrationError


def _logo(size=(600, 200)):
    output = BytesIO()
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((240, 200, 40, 255), (50, 50, 550, 150))
    image.save(output, format="PNG")
    return output.getvalue()


class KindsJellyfin:
    """A Jellyfin with a picture per type; a title without one answers 404."""

    def __init__(self, **pictures):
        self.pictures = dict(pictures)
        self.uploads, self.deleted, self.downloaded = [], [], []

    def image(self, _item_id, kind="Primary"):
        if kind not in self.pictures:
            raise IntegrationError("Jellyfin GET image failed", 404)
        return self.pictures[kind]

    def put_image(self, _item_id, data, kind="Primary", mimetype="image/jpeg"):
        self.pictures[kind] = data
        self.uploads.append((kind, mimetype))

    def delete_image(self, _item_id, kind, index):
        self.pictures.pop(kind, None)
        self.deleted.append((kind, index))

    def image_list(self, _item_id):
        return []

    def remote_images(self, _item_id, _kind="Primary"):
        return []

    def download_remote_image(self, _item_id, image, _kind="Primary"):
        self.downloaded.append(image["Url"])

    def users(self):
        return [{"Id": u, "PrimaryImageTag": None} for u in ("u1", "u2")]

    def item_details(self, ids):
        return [{"Id": i, "ImageTags": {"Primary": "tag-1"}} for i in ids]


def _fake(monkeypatch, **pictures):
    jellyfin = KindsJellyfin(**pictures)
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    return jellyfin


def test_a_background_goes_on_and_the_original_comes_back(app, monkeypatch):
    _library, media, _part = _movie()
    original = _picture("#334455", size=(1920, 1080))
    jellyfin = _fake(monkeypatch, Primary=_picture("#223344"), Backdrop=original)
    record = custom_posters.apply(
        media, _picture("#aa3322", size=(1920, 1080)), "Uploaded", kind="Backdrop"
    )
    assert record.image_type == "Backdrop" and record.original_path
    assert jellyfin.uploads == [("Backdrop", "image/jpeg")]
    assert custom_posters.active(media.id) is None  # the poster is untouched
    assert custom_posters.active(media.id, kind="Backdrop") == record
    said = custom_posters.restore(record)
    assert "original background" in said
    assert custom_posters._same_picture(jellyfin.pictures["Backdrop"], original)


def test_a_logo_stays_see_through_and_one_it_never_had_is_removed(app, monkeypatch):
    _library, media, _part = _movie()
    jellyfin = _fake(monkeypatch, Primary=_picture("#223344"))  # no logo
    record = custom_posters.apply(media, _logo(), "Uploaded", kind="Logo")
    assert record.original_path == "" and record.image_path.endswith(".png")
    assert jellyfin.uploads == [("Logo", "image/png")]
    kept = Image.open(BytesIO(jellyfin.pictures["Logo"]))
    assert kept.mode == "RGBA" and kept.getpixel((5, 5))[3] == 0
    said = custom_posters.restore(record)
    assert "no logo again" in said and jellyfin.deleted == [("Logo", 0)]


def test_a_word_in_the_file_name_says_which_picture(app, monkeypatch, tmp_path):
    assert custom_posters.split_kind("08_the-big-lebowski-1998.thumb.jpg") == (
        "Thumb",
        "08_the-big-lebowski-1998.jpg",
    )
    assert custom_posters.split_kind("Film (1998).logo.png") == (
        "Logo",
        "Film (1998).png",
    )
    assert custom_posters.split_kind("Mr. Robot.jpg") == ("Primary", "Mr. Robot.jpg")
    assert custom_posters.split_kind("08_film.jpg") == ("Primary", "08_film.jpg")
    _library, media, _part = _movie()
    jellyfin = _fake(monkeypatch, Primary=_picture("#223344"))
    custom_posters.save_settings(str(tmp_path))
    settled = time.time() - 60
    files = {
        "01_film.backdrop.jpg": _picture("#aa3322", size=(1920, 1080)),
        "01_film.logo.png": _logo(),
        "01_film.banner.jpg": _picture("#3322aa", size=(1000, 185)),
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        os.utime(tmp_path / name, (settled, settled))
    assert custom_posters.sweep() == 3
    assert {kind for kind, _mime in jellyfin.uploads} == {"Backdrop", "Logo", "Banner"}
    assert custom_posters.active(media.id) is None
    assert custom_posters.active(media.id, kind="Logo").source == "01_film.logo.png"


def test_prompts_ask_for_a_matching_set(app, monkeypatch):
    from curatarr.models import UserMediaState

    _library, media, _part = _movie("/movies/The Big Lebowski (1998)/Film.mkv")
    media.title = "The Big Lebowski"
    db.session.add(
        UserMediaState(media_identity_id=media.id, jellyfin_user_id="u1", favorite=True)
    )
    db.session.commit()
    _fake(monkeypatch, Primary=_picture("#223344"))
    kinds = ["Primary", "Thumb", "Backdrop", "Logo"]
    titles = custom_posters.prompt_titles("favourites", True, kinds)
    described = custom_posters.describe(titles, kinds)
    files = described[0]["files"]
    assert files["Thumb"] == "01_the-big-lebowski-1998.thumb.jpg"
    assert files["Logo"] == "01_the-big-lebowski-1998.logo.png"
    text = custom_posters.prompts(
        described, batch=10, photos="", people="", output="/posters"
    )[0]
    assert "matching set" in text and "transparent background" in text
    assert "own logo lettering" in text and "Never a plain or generic font" in text
    assert "Wide tile → 01_the-big-lebowski-1998.thumb.jpg" in text
    assert "Banner:" not in text  # not asked for
    # Each file name finds its title and type.
    for kind, name in files.items():
        found, plain = custom_posters.split_kind(name)
        assert found == kind
        assert custom_posters.match(plain, custom_posters._titles())[0] == media
    # Skipped only once it has every picture asked for.
    custom_posters.apply(media, _picture("#cc2222"), "Uploaded")
    assert custom_posters.prompt_titles("favourites", True, kinds) == [media]
    assert custom_posters.prompt_titles("favourites", True, ["Primary"]) == []


def test_prompts_ask_only_for_whats_missing(app, monkeypatch, tmp_path):
    from curatarr.models import UserMediaState

    _library, media, part = _movie("/movies/The Big Lebowski (1998)/Film.mkv")
    media.title = "The Big Lebowski"
    db.session.add(
        UserMediaState(media_identity_id=media.id, jellyfin_user_id="u1", favorite=True)
    )
    db.session.commit()
    _fake(monkeypatch, Primary=_picture("#223344"))
    custom_posters.save_settings(str(tmp_path))
    (tmp_path / "07_the-big-lebowski.jpg").write_bytes(_picture("#cc2222"))
    custom_posters.apply(media, _picture("#cc2222"), "07_the-big-lebowski.jpg")
    kinds = ["Primary", "Thumb", "Logo"]
    titles = custom_posters.prompt_titles("favourites", True, kinds)
    entry = custom_posters.describe(titles, kinds, skip_done=True)[0]
    # The poster's done: not asked for again; the others go beside it.
    assert entry["files"] == {
        "Thumb": "07_the-big-lebowski.thumb.jpg",
        "Logo": "07_the-big-lebowski.logo.png",
    }
    assert entry["made"] == "07_the-big-lebowski.jpg"
    text = custom_posters.prompts(
        [entry], batch=5, photos="", people="", output=str(tmp_path)
    )[0]
    assert "Its poster is already made" in text and "Poster →" not in text
    assert "Poster:" not in text  # its description isn't asked for either
    # Without skipping, the whole set is asked for again.
    again = custom_posters.describe(titles, kinds)[0]
    assert set(again["files"]) == set(kinds) and again["made"] is None
    # A deleted title needs no pictures.
    part.has_file = False
    db.session.commit()
    assert custom_posters.prompt_titles("favourites", False, kinds) == []


def test_the_plugin_lists_each_accounts_pictures_by_type(app, client, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    _fake(monkeypatch, Primary=_picture("#223344", size=(400, 600)))
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex"})
    set_setting("webhook_token", "secret")
    db.session.commit()
    custom_posters.apply(media, _picture("#cc2222"), "01_film.jpg", user_id="u1")
    custom_posters.apply(
        media,
        _picture("#22aa22", size=(1920, 1080)),
        "01_film.backdrop.jpg",
        user_id="u1",
        kind="Backdrop",
    )
    custom_posters.apply(media, _logo(), "01_film.logo.png", user_id="u2", kind="Logo")
    gallery = client.get("/library/posters").text
    assert "Background · Sam" in gallery and "Logo · Alex" in gallery
    assert gallery.count("data-full=") == 3 and "lightbox.js" in gallery
    page = client.get(f"/titles/{media.id}").text
    assert "sees their own background" in page and "sees their own logo" in page
    # Version 1 (plugins before 0.8.0.0): posters only.
    old = custom_posters.plugin_list()
    assert [(e["user"], e["wide"]) for e in old] == [("u1", True)]
    listing = client.get(
        "/api/v1/jellyfin/posters?v=2", headers={"X-Curatarr-Token": "secret"}
    ).get_json()
    assert listing["version"] == 2
    kinds = {(e["user"], e["kind"]): e for e in listing["posters"]}
    # Sam: the poster (with a wide tile made from it, its "wide" version, under
    # its tag as plugin 0.7.0.0 had it, so those files are kept) and the
    # background; Alex: only the logo (no poster of their own to make others from).
    assert set(kinds) == {("u1", "Primary"), ("u1", "Backdrop"), ("u2", "Logo")}
    assert kinds["u1", "Primary"]["wide"]
    assert kinds["u1", "Primary"]["tag"] == old[0]["tag"]
    wide = Image.open(
        BytesIO(custom_posters.plugin_picture(kinds["u1", "Primary"]["id"], wide=True))
    )
    assert wide.size == (1280, 720)
    background = custom_posters.plugin_picture(kinds["u1", "Backdrop"]["id"])
    assert Image.open(BytesIO(background)).getpixel((960, 540))[1] > 150  # its own
    logo = client.get(
        f"/api/v1/jellyfin/posters/{kinds['u2', 'Logo']['id']}.jpg",
        headers={"X-Curatarr-Token": "secret"},
    )
    assert logo.mimetype == "image/png"
    # Made-up backgrounds and banners when chosen; Jellyfin's own wide tile.
    client.post(
        "/library/posters/fallbacks",
        data={"Thumb": "own", "Backdrop": "blurred", "Banner": "widened"},
    )
    assert custom_posters.fallbacks() == {
        "Thumb": "own",
        "Backdrop": "blurred",
        "Banner": "widened",
    }
    kinds = {(e["user"], e["kind"]): e for e in custom_posters.plugin_list(2)}
    assert ("u1", "Thumb") not in kinds and not kinds["u1", "Primary"]["wide"]
    banner = Image.open(
        BytesIO(custom_posters.plugin_picture(kinds["u1", "Banner"]["id"]))
    )
    assert banner.size == (1000, 185)
    assert [e["wide"] for e in custom_posters.plugin_list()] == [False]


def test_an_accounts_wide_tile_counts_down_and_carries_the_ribbon(app, monkeypatch):
    from curatarr.models import PosterSnapshot, PurgeCandidate
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    _fake(monkeypatch, Primary=_picture("#223344", size=(400, 600)))
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex"})
    db.session.commit()
    thumb = _picture("#2244aa", size=(1280, 720))
    custom_posters.apply(media, thumb, "Uploaded", user_id="u2", kind="Thumb")
    _favourite(media, "u1")
    custom_posters.save_ribbon_settings(
        {"enabled": "on", "see-u2-u1": "on"}, [("u1", "Sam"), ("u2", "Alex")]
    )
    entry = next(e for e in custom_posters.plugin_list(2) if e["kind"] == "Thumb")
    poster = next(e for e in custom_posters.plugin_list(2) if e["kind"] == "Primary")
    assert not poster["wide"]  # its own wide tile, not one made from the poster
    plain = custom_posters.plugin_picture(entry["id"])
    candidate = PurgeCandidate(
        media_identity_id=media.id,
        state="LEAVING_SOON",
        reason_code="unwatched",
        reason_text="test",
        reclaimable_bytes=0,
        score=0,
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
    leaving = next(e for e in custom_posters.plugin_list(2) if e["kind"] == "Thumb")
    assert leaving["tag"] != entry["tag"]  # apps fetch it again
    counted = Image.open(BytesIO(custom_posters.plugin_picture(leaving["id"])))
    ribboned = Image.open(BytesIO(plain))
    # The countdown runs along the bottom; the ribbon moves to the top.
    bottom = (counted.width // 2, counted.height - 5)
    green = lambda image, at: image.getpixel(at)[1]
    assert green(counted, bottom) < 80  # red
    assert green(ribboned, bottom) > 150  # gold
    assert green(counted, (counted.width // 2, 5)) > 150  # gold, up top


def test_a_deleted_titles_pictures_are_retired(app, monkeypatch):
    from pathlib import Path

    from curatarr.models import LifecycleAction, PurgeCandidate

    _library, media, part = _movie()
    _fake(monkeypatch, Primary=_picture("#223344"))
    poster = custom_posters.apply(media, _picture("#cc2222"), "Up", user_id="u1")
    logo = custom_posters.apply(media, _logo(), "Up", user_id="u1", kind="Logo")
    candidate = PurgeCandidate(
        media_identity_id=media.id,
        state="COMPLETED",
        reason_code="unwatched",
        reason_text="test",
        reclaimable_bytes=0,
        score=0,
    )
    db.session.add(candidate)
    db.session.commit()
    assert custom_posters.retire_gone() == 0  # a dry run: its file's still there
    part.has_file = False
    db.session.commit()
    assert custom_posters.plugin_list(2)
    assert custom_posters.retire_gone() == 1
    assert custom_posters.plugin_list(2) == []
    assert not Path(poster.image_path).exists() and not Path(logo.image_path).exists()
    said = db.session.query(LifecycleAction).filter_by(
        action_type="custom_pictures_retired"
    )
    assert "2 custom pictures were removed" in said.one().reason_text
    assert custom_posters.retire_gone() == 0


def test_prompts_keep_the_reference_people_recognisable(app, client, tmp_path):
    entry = {
        "number": 1,
        "title": "Film",
        "year": 2016,
        "kind": "film",
        "genres": [],
        "overview": "",
        "file": "01_film.jpg",
        "files": {"Primary": "01_film.jpg", "Backdrop": "01_film.backdrop.jpg"},
        "made": None,
    }
    text = custom_posters.prompts(
        [entry], batch=5, photos="/photos", people="Sam and Alex", output="/out"
    )[0]
    assert "Reference photos of Sam and Alex" in text and "/photos" in text
    assert "in place of the actors" in text
    assert "from image inputs" in text and "side by side" in text
    assert "Make only the files listed" in text
    assert "Use all of their photos" in text and "plus the title's poster" in text
    assert "head-and-shoulders" not in text  # nothing made that Jellyfin doesn't use
    assert "a different photo leads each time" in text
    assert "large and clear enough to recognise" in text  # the background
    # A photos folder that isn't there is saved, with a warning.
    response = client.post(
        "/library/posters/prompts",
        data={"titles": "favourites", "batch": "5", "photos": "/no/such/folder"},
        follow_redirects=True,
    )
    assert "find the reference photos folder" in response.text
    assert custom_posters.prompt_options()["photos"] == "/no/such/folder"
    client.post(
        "/library/posters/prompts",
        data={"titles": "favourites", "batch": "5", "photos": str(tmp_path)},
    )
    assert custom_posters.prompt_options()["photos"] == str(tmp_path)


def test_prompts_ask_for_an_extreme_take_unless_told_otherwise(app, client, tmp_path):
    entry = {
        "number": 1,
        "title": "Film",
        "year": 2016,
        "kind": "film",
        "genres": [],
        "overview": "",
        "file": "01_film.jpg",
        "files": {"Primary": "01_film.jpg"},
        "made": None,
    }
    options = custom_posters.prompt_options()
    assert "extreme, heightened take" in options["style"]  # the default
    text = custom_posters.prompts(
        [entry], batch=5, photos="", people="", output="/out", style=options["style"]
    )[0]
    assert "The look: An extreme" in text and "sexual tension" in text
    assert "The look" in client.get("/library/posters").text
    client.post(
        "/library/posters/prompts",
        data={"titles": "favourites", "batch": "5", "style": "Quiet,  painterly."},
    )
    assert custom_posters.prompt_options()["style"] == "Quiet, painterly."
    client.post("/library/posters/prompts", data={"titles": "favourites", "batch": "5"})
    assert custom_posters.prompt_options()["style"] == ""  # emptied: none
    plain = custom_posters.prompts(
        [entry], batch=5, photos="", people="", output="/out", style=""
    )[0]
    assert "The look" not in plain
