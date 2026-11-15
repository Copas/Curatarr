"""Overview's Recently watched (Jellyfin's activity log, a box per person)
and Recently added."""

from datetime import timedelta

import pytest

from curatarr import db, recent
from curatarr.integrations import IntegrationError
from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart, utcnow
from curatarr.services import set_setting


def _library(media_type):
    library = Library(
        jellyfin_library_id=media_type, name=media_type.title(), media_type=media_type
    )
    db.session.add(library)
    db.session.flush()
    return library


def _film(library, title, acquired=None, jellyfin_id=None):
    media = MediaIdentity(
        library_id=library.id, media_type="movie", title=title, jellyfin_id=jellyfin_id
    )
    db.session.add(media)
    db.session.flush()
    db.session.add(
        MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            acquired_at=acquired,
        )
    )
    db.session.flush()
    return media


def _show(library, title, episodes, acquired=None):
    media = MediaIdentity(library_id=library.id, media_type="series", title=title)
    db.session.add(media)
    db.session.flush()
    parts = []
    for season, episode in episodes:
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=season,
            episode_number=episode,
            jellyfin_id=f"ep{season}x{episode}",
            has_file=True,
            acquired_at=acquired,
        )
        db.session.add(part)
        parts.append(part)
    db.session.flush()
    return media, parts


def _stamp(moment):
    # As Jellyfin writes it: seven decimal places and a Z.
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def _play(log, user, name, item, title, device, start, minutes):
    """A start and a stop in the activity log, newest first like Jellyfin."""
    log[:0] = [
        {
            "Type": "VideoPlaybackStopped",
            "UserId": user,
            "ItemId": item,
            "Date": _stamp(start + timedelta(minutes=minutes)),
            "Name": f"{name} has finished playing {title} on {device}",
        },
        {
            "Type": "VideoPlayback",
            "UserId": user,
            "ItemId": item,
            "Date": _stamp(start),
            "Name": f"{name} is playing {title} on {device}",
        },
    ]


class FakeJellyfin:
    def __init__(self, log, played=None, fail=False):
        self.log, self.played_items, self.fail, self.asked = log, played or {}, fail, 0

    def activity_log(self, since):
        self.asked += 1
        if self.fail:
            raise IntegrationError("Jellyfin is down")
        return list(self.log)

    def played(self, user_id, item_ids):
        return set(self.played_items.get(user_id, ())) & set(item_ids)


@pytest.fixture
def jellyfin(monkeypatch):
    def install(fake):
        monkeypatch.setattr(recent, "client", lambda kind: fake)
        return fake

    return install


def test_a_box_per_person_from_jellyfins_activity_log(app, jellyfin):
    set_setting("jellyfin_user_names", {"u1": "Alex", "u2": "Sam"})
    now = utcnow()
    films, tv = _library("movies"), _library("tv")
    heat = _film(films, "Heat", jellyfin_id="heat")
    _show(tv, "Bluey", [(1, 1), (1, 2)])
    db.session.commit()
    log = []
    _play(
        log,
        "u1",
        "Alex",
        "heat",
        "Heat",
        "Living Room TV",
        now - timedelta(hours=5),
        170,
    )
    _play(
        log,
        "u2",
        "Sam",
        "ep1x1",
        "Bluey - Magic Xylophone",
        "Phone",
        now - timedelta(hours=2),
        7,
    )
    _play(
        log,
        "u2",
        "Sam",
        "ep1x2",
        "Bluey - Hospital",
        "Phone",
        now - timedelta(hours=1),
        3,
    )
    jellyfin(FakeJellyfin(log, played={"u1": {"heat"}, "u2": {"ep1x1"}}))

    people = recent.watched(now)
    assert [p["name"] for p in people] == ["Sam", "Alex"]
    hospital, xylophone = people[0]["plays"]  # each episode its own line
    assert hospital["title"] == "Bluey"
    assert hospital["episode"] == "S01E02 · Hospital"
    assert hospital["device"] == "Phone"
    assert hospital["finished"] is False and xylophone["finished"] is True
    (film,) = people[1]["plays"]
    assert film["media_id"] == heat.id and film["finished"] is True
    assert film["device"] == "Living Room TV" and film["episode"] is None


def test_a_play_of_a_moment_is_not_listed_and_picking_up_again_is_one_line(
    app, jellyfin
):
    set_setting("jellyfin_user_names", {"u1": "Alex"})
    now = utcnow()
    log = []
    _play(
        log, "u1", "Alex", "a", "Opened by mistake", "TV", now - timedelta(hours=4), 0
    )
    _play(log, "u1", "Alex", "b", "Watched", "TV", now - timedelta(hours=3), 40)
    _play(log, "u1", "Alex", "b", "Watched", "TV", now - timedelta(hours=2), 50)
    jellyfin(FakeJellyfin(log))

    (alex,) = recent.watched(now)
    assert [p["title"] for p in alex["plays"]] == ["Watched"]
    assert alex["plays"][0]["media_id"] is None  # not in Curatarr's libraries


def test_a_play_with_no_stop_logged_still_counts(app, jellyfin):
    """The app dropped off, or it's still playing: Jellyfin logs the start only."""
    set_setting("jellyfin_user_names", {"u1": "Alex"})
    now = utcnow()
    log = []
    _play(log, "u1", "Alex", "a", "Earlier", "TV", now - timedelta(hours=3), 30)
    log.insert(
        0,
        {
            "Type": "VideoPlayback",
            "UserId": "u1",
            "ItemId": "b",
            "Date": _stamp(now - timedelta(hours=1)),
            "Name": "Alex is playing Dropped Off on Laptop",
        },
    )
    jellyfin(FakeJellyfin(log))
    (alex,) = recent.watched(now)
    assert [(p["title"], p["device"]) for p in alex["plays"]] == [
        ("Dropped Off", "Laptop"),
        ("Earlier", "TV"),
    ]


def test_hidden_accounts_are_left_out(app, jellyfin):
    set_setting("jellyfin_user_names", {"u1": "Alex", "u9": "Guest"})
    set_setting("jellyfin_hidden_user_ids", ["u9"])
    set_setting("hidden_accounts", "leave_out")
    now = utcnow()
    log = []
    _play(
        log, "u9", "Guest", "a", "Seen by a guest", "TV", now - timedelta(hours=1), 30
    )
    jellyfin(FakeJellyfin(log))
    assert recent.watched(now) == []


def test_jellyfin_is_asked_once_a_couple_of_minutes_and_a_failure_is_no_list(
    app, jellyfin
):
    set_setting("jellyfin_user_names", {"u1": "Alex"})
    now = utcnow()
    log = []
    _play(log, "u1", "Alex", "a", "Heat", "TV", now - timedelta(hours=1), 30)
    fake = jellyfin(FakeJellyfin(log))
    assert recent.watched(now) and recent.watched(now + timedelta(seconds=30))
    assert fake.asked == 1
    fake.fail = True
    # Down: the last list stays rather than the section vanishing.
    assert recent.watched(now + timedelta(minutes=5))[0]["name"] == "Alex"
    app.extensions.pop("curatarr_recently_watched")
    assert recent.watched(now + timedelta(minutes=6)) == []


def _seen(media, at):
    """Curatarr first looked at the film's file at ``at``."""
    part = next(p for p in media.parts if p.kind == "movie_file")
    db.session.add(
        ChapterState(media_part_id=part.id, media_identity_id=media.id, created_at=at)
    )


def test_recently_added_is_whats_new_to_the_library(app):
    """A show's new episodes are one line; a film whose file replaced an
    earlier one (an upgrade, or a copy Curatarr replaced) isn't new."""
    now = utcnow()
    films, tv = _library("movies"), _library("tv")
    _show(tv, "Good Eats", [(1, 1), (1, 2), (1, 3)], acquired=now - timedelta(hours=1))
    new = _film(films, "New Film", acquired=now - timedelta(hours=2))
    _seen(new, now - timedelta(hours=2) + timedelta(minutes=20))
    swapped = _film(films, "Swapped", acquired=now - timedelta(hours=3))
    _seen(swapped, now - timedelta(days=30))  # a copy was here a month ago
    _film(films, "Old", acquired=now - timedelta(days=30))
    db.session.commit()

    added = recent.added(now)
    assert [r["title"] for r in added["shows"]] == ["Good Eats"]
    assert [r["title"] for r in added["films"]] == ["New Film"]
    show = added["shows"][0]
    assert show["episodes"] == 3 and (show["first"], show["last"]) == (
        "S01E01",
        "S01E03",
    )
    assert added["films"][0]["media_id"] == new.id


def test_films_and_shows_each_get_their_own_share(app):
    """A busy week of episodes doesn't push every film out, nor the reverse."""
    now = utcnow()
    films, tv = _library("movies"), _library("tv")
    for number in range(4):
        _show(tv, f"Show {number}", [(1, 1)], acquired=now - timedelta(hours=number))
    _film(films, "Older Film", acquired=now - timedelta(days=3))
    db.session.commit()
    added = recent.added(now, shown=2)
    assert [r["title"] for r in added["shows"]] == ["Show 0", "Show 1"]
    assert [r["title"] for r in added["films"]] == ["Older Film"]


def test_overview_shows_both_lists(app, client, jellyfin):
    set_setting("jellyfin_user_names", {"u1": "Alex"})
    now = utcnow()
    films = _library("movies")
    _film(films, "Heat", acquired=now - timedelta(hours=1), jellyfin_id="heat")
    db.session.commit()
    log = []
    _play(
        log,
        "u1",
        "Alex",
        "heat",
        "Heat",
        "Living Room TV",
        now - timedelta(hours=1),
        40,
    )
    jellyfin(FakeJellyfin(log, played={"u1": {"heat"}}))

    page = client.get("/").text
    assert "Recently watched</h2>" in page and "Recently added</h2>" in page
    assert "Alex" in page and ">Heat</a>" in page
    assert 'class="reel-title">Heat</span>' in page and "/posters/" in page
    assert ">Movies</h3>" in page and ">Shows</h3>" not in page  # no new shows
    assert "Living Room TV" in page and 'aria-label="Watched"' in page


def test_posters_are_small_from_jellyfin_and_kept_a_few_minutes(
    app, client, monkeypatch
):
    """Overview shows dozens of posters: fetching and re-encoding full-size
    ones on every request held up every other page."""
    from io import BytesIO

    from PIL import Image

    asked = []

    class Images:
        def image(self, item_id, kind="Primary", max_width=None):
            asked.append(max_width)
            if item_id == "png":
                out = BytesIO()
                Image.new("RGB", (4, 6)).save(out, format="PNG")
                return out.getvalue()
            return b"\xff\xd8 a small jpeg"

    monkeypatch.setattr("curatarr.services.client", lambda kind: Images())
    films = _library("movies")
    jpeg = _film(films, "Heat", jellyfin_id="jpg")
    png = _film(films, "Ran", jellyfin_id="png")
    db.session.commit()

    first = client.get(f"/posters/{jpeg.id}")
    again = client.get(f"/posters/{jpeg.id}")
    assert first.data == again.data == b"\xff\xd8 a small jpeg"  # passed through
    assert asked == [400]  # once, scaled by Jellyfin
    converted = client.get(f"/posters/{png.id}")
    assert converted.mimetype == "image/jpeg" and converted.data[:2] == b"\xff\xd8"
