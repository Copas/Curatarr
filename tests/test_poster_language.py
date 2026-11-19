"""Posters in another language swapped for ones in the metadata language."""

from datetime import timedelta
from io import BytesIO

from PIL import Image, ImageDraw

from curatarr import db, poster_language
from curatarr.models import utcnow
from curatarr.services import set_setting, setting


def _poster(colour, text=None):
    image = Image.new("RGB", (200, 300), colour)
    if text:
        ImageDraw.Draw(image).rectangle((20, 200, 180, 260), fill=text)
    out = BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


ART = {
    "https://img/en.jpg": _poster("#335577", "#ffffff"),
    "https://img/zh.jpg": _poster("#335577", "#cc0000"),
    "https://img/plain.jpg": _poster("#aa7733"),
    "https://img/other.jpg": _poster("#22aa22"),
}


def fetch(url):
    return ART.get(url)


def image(url, language):
    return {"Url": url, "Language": language, "ProviderName": "TheMovieDb"}


class Jellyfin:
    def __init__(self, posters, offered):
        self.posters = posters  # item id -> url of the current poster
        self.offered = offered
        self.remote_calls = 0

    def configuration(self):
        return {"PreferredMetadataLanguage": "en"}

    premiered = "2026-01-01"

    def titles_with_posters(self, start=0, limit=200):
        items = [
            {
                "Id": i,
                "Name": f"Film {i}",
                "ImageTags": {"Primary": self.posters[i].rsplit("/", 1)[-1]},
                "PremiereDate": self.premiered,
            }
            for i in sorted(self.posters)
        ]
        page = items[start : start + limit]
        return {"Items": page, "TotalRecordCount": len(items)}

    def remote_images(self, item_id, _kind="Primary"):
        self.remote_calls += 1
        return [dict(i) for i in self.offered[item_id]]

    def image(self, item_id, _kind="Primary"):
        return ART[self.posters[item_id]]

    def download_remote_image(self, item_id, chosen, _kind="Primary"):
        self.posters[item_id] = chosen["Url"]


def _setup(monkeypatch, posters, offered):
    jellyfin = Jellyfin(posters, offered)
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    return jellyfin


BOTH = [image("https://img/en.jpg", "en"), image("https://img/zh.jpg", "zh")]


def test_a_foreign_poster_is_swapped_for_one_in_the_metadata_language(app, monkeypatch):
    with app.app_context():
        jellyfin = _setup(
            monkeypatch, {"a" * 32: "https://img/zh.jpg"}, {"a" * 32: BOTH}
        )
        assert poster_language.sweep(fetch=fetch) == 1
        assert jellyfin.posters["a" * 32] == "https://img/en.jpg"


def test_english_hand_picked_and_textless_posters_are_kept(app, monkeypatch):
    with app.app_context():
        posters = {
            "a" * 32: "https://img/en.jpg",
            "b" * 32: "https://img/other.jpg",  # matches nothing offered
            "c" * 32: "https://img/plain.jpg",
        }
        offered = {
            "a" * 32: BOTH,
            "b" * 32: BOTH,
            "c" * 32: [image("https://img/plain.jpg", ""), *BOTH],
        }
        jellyfin = _setup(monkeypatch, dict(posters), offered)
        assert poster_language.sweep(fetch=fetch) == 0
        assert jellyfin.posters == posters
        assert set(setting(poster_language.KEY)["kept"]) == set(posters)


def test_without_one_in_our_language_it_waits_and_looks_again(app, monkeypatch):
    with app.app_context():
        item = "a" * 32
        jellyfin = _setup(
            monkeypatch,
            {item: "https://img/zh.jpg"},
            {item: [image("https://img/zh.jpg", "zh")]},
        )
        now = utcnow()
        assert poster_language.sweep(now=now, fetch=fetch) == 0
        assert item in setting(poster_language.KEY)["waiting"]
        # A day later the pass runs again, but the title isn't due yet.
        poster_language.sweep(now=now + timedelta(days=1), fetch=fetch)
        assert jellyfin.remote_calls == 1
        jellyfin.offered[item] = BOTH  # an English poster appeared
        assert poster_language.sweep(now=now + timedelta(days=4), fetch=fetch) == 1
        assert jellyfin.posters[item] == "https://img/en.jpg"


def test_runs_once_a_day_and_can_be_turned_off(app, monkeypatch):
    with app.app_context():
        item = "a" * 32
        jellyfin = _setup(monkeypatch, {item: "https://img/en.jpg"}, {item: BOTH})
        now = utcnow()
        poster_language.sweep(now=now, fetch=fetch)
        poster_language.sweep(now=now + timedelta(hours=2), fetch=fetch)
        assert jellyfin.remote_calls == 1
        set_setting("curation", {"poster_language": False})
        db.session.commit()
        jellyfin.posters[item] = "https://img/zh.jpg"
        set_setting(poster_language.KEY, {})
        assert poster_language.sweep(now=now + timedelta(days=2), fetch=fetch) == 0
        assert jellyfin.posters[item] == "https://img/zh.jpg"


def test_a_poster_jellyfin_changes_later_is_looked_at_again(app, monkeypatch):
    """Found fine once, then a refresh (or a new season) brought a foreign
    one: its tag changed, so the next pass looks again, however old the
    title is."""
    with app.app_context():
        item = "a" * 32
        jellyfin = _setup(monkeypatch, {item: "https://img/en.jpg"}, {item: BOTH})
        jellyfin.premiered = "2019-05-01"  # long out of the first year
        now = utcnow()
        assert poster_language.sweep(now=now, fetch=fetch) == 0
        poster_language.sweep(now=now + timedelta(days=1), fetch=fetch)
        assert jellyfin.remote_calls == 1  # unchanged: not fetched again
        jellyfin.posters[item] = "https://img/zh.jpg"
        assert poster_language.sweep(now=now + timedelta(days=2), fetch=fetch) == 1
        assert jellyfin.posters[item] == "https://img/en.jpg"


def test_a_foreign_poster_below_the_most_liked_is_still_recognised(app, monkeypatch):
    """Ours was offered 20th: past the first 15, so it used to count as hand
    picked and stay."""
    with app.app_context():
        item = "a" * 32
        filler = [image(f"https://img/none{n}.jpg", "fr") for n in range(19)]
        _setup(
            monkeypatch,
            {item: "https://img/zh.jpg"},
            {item: [image("https://img/en.jpg", "en"), *filler, BOTH[1]]},
        )
        assert poster_language.sweep(fetch=fetch) == 1


def test_titles_kept_before_tags_are_looked_at_once_more(app, monkeypatch):
    with app.app_context():
        item = "a" * 32
        jellyfin = _setup(monkeypatch, {item: "https://img/zh.jpg"}, {item: BOTH})
        set_setting(poster_language.KEY, {"kept": {item: "2026-10-23T11:43:11"}})
        db.session.commit()
        assert poster_language.sweep(fetch=fetch) == 1
        assert jellyfin.posters[item] == "https://img/en.jpg"


def _striped(text):
    """A poster with detail all the way down, as real ones have."""
    image = Image.new("RGB", (200, 300), "#eeeeee")
    draw = ImageDraw.Draw(image)
    for top in range(0, 300, 24):
        draw.rectangle((0, top, 200, top + 11), fill="#202020")
    draw.rectangle((20, 40, 180, 80), fill=text)  # the title, in its language
    return image


def _jpeg(image):
    out = BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


def test_jellyfins_copy_in_another_shape_is_still_recognised(app, monkeypatch):
    """Jellyfin keeps some posters at 1000x1426, a crop of TMDB's 2:3: the
    same picture squeezed into one grid looked like no poster offered, so a
    Polish one stayed as if picked by hand."""
    with app.app_context():
        item = "a" * 32
        ART["https://img/s-en.jpg"] = _jpeg(_striped("#ffffff"))
        ART["https://img/s-pl.jpg"] = _jpeg(_striped("#cc0000"))
        # Jellyfin's copy: the Polish one, cut to 200x285 near its middle
        ART["https://img/s-jellyfin.jpg"] = _jpeg(
            _striped("#cc0000").crop((0, 9, 200, 294))
        )
        offered = [
            image("https://img/s-en.jpg", "en"),
            image("https://img/s-pl.jpg", "pl"),
        ]
        jellyfin = _setup(
            monkeypatch, {item: "https://img/s-jellyfin.jpg"}, {item: offered}
        )
        assert poster_language.sweep(fetch=fetch) == 1
        assert jellyfin.posters[item] == "https://img/s-en.jpg"


def test_titles_kept_under_older_matching_are_looked_at_again_at_once(app, monkeypatch):
    """The first pass with tags kept foreign posters the old matching
    couldn't recognise, and a kept poster is only looked at again when it
    changes: a new matching rule looks at everything again, straight away."""
    with app.app_context():
        item = "a" * 32
        jellyfin = _setup(monkeypatch, {item: "https://img/zh.jpg"}, {item: BOTH})
        now = utcnow()
        set_setting(
            poster_language.KEY,
            {"kept": {item: "zh.jpg"}, "finished_at": now.isoformat()},
        )
        db.session.commit()
        assert poster_language.sweep(now=now + timedelta(hours=1), fetch=fetch) == 1
        assert jellyfin.posters[item] == "https://img/en.jpg"
        assert setting(poster_language.KEY)["rule"] == poster_language.RULE
