"""A title's page says what it is and who's watched it (title_info.py)."""

from datetime import timedelta

from test_acquisition_reconcile import _show

from curatarr import db
from curatarr.models import EpisodeUserState, UserMediaState
from curatarr.services import IntegrationError, set_setting, utcnow


class FakeJellyfin:
    def __init__(self, item=None, fails=False):
        self.item, self.fails, self.asked = item or {}, fails, 0

    def item_about(self, _item_id):
        self.asked += 1
        if self.fails:
            raise IntegrationError("Jellyfin is down")
        return self.item


ITEM = {
    "Overview": "Five comedians take on absurd tasks set by a stern judge.",
    "Genres": ["Comedy", "Game Show"],
    "ProductionYear": 2020,
    "OfficialRating": "TV-14",
    "CommunityRating": 8.4,
    "RunTimeTicks": 46 * 600_000_000,
    "Taglines": ["All will be judged."],
    "Studios": [{"Name": "Avalon"}],
    "PremiereDate": "2020-05-01T00:00:00Z",
    "Status": "Continuing",
}


def _watched(media, parts, who, episodes, *, last, favorite=False, rewatched=0):
    db.session.add(
        UserMediaState(
            media_identity_id=media.id,
            jellyfin_user_id=who,
            last_played_at=last,
            favorite=favorite,
            play_count=rewatched,
        )
    )
    for n, key in enumerate(episodes):
        db.session.add(
            EpisodeUserState(
                media_part_id=parts[key].id,
                jellyfin_user_id=who,
                played=True,
                last_played_at=last - timedelta(hours=n),
            )
        )


def test_a_shows_page_says_what_it_is_and_who_watched_it(app, client, monkeypatch):
    jellyfin = FakeJellyfin(ITEM)
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    set_setting("jellyfin_user_names", {"u1": "Alex", "u2": "Sam"})
    media, parts = _show("Tasks", [(1, 1, True), (1, 2, True), (1, 3, True)])
    now = utcnow()
    _watched(media, parts, "u1", [(1, 3), (1, 2), (1, 1)], last=now, rewatched=1)
    _watched(media, parts, "u2", [(1, 1)], last=now - timedelta(days=3), favorite=True)
    db.session.commit()
    page = client.get(f"/titles/{media.id}").text
    # About: Jellyfin's plot, genres and facts
    assert ITEM["Overview"] in page and "All will be judged." in page
    assert '<span class="chip">Comedy</span>' in page
    for fact in ("2020–", "TV-14", "★ 8.4", "46 min an episode", "Still running"):
        assert fact in page
    # Watching: most recent first, progress, favourite, rewatching
    alex, sam = page.index("<strong>Alex</strong>"), page.index("<strong>Sam</strong>")
    assert alex < sam
    assert "3 of 3 episodes (all)" in page and "1 of 3 episodes" in page
    assert "rewatched 1 episode" in page and "favourite" in page[sam:]
    assert "Recently watched episodes" in page and "S01E03" in page
    # kept a while: the next look doesn't ask Jellyfin again
    client.get(f"/titles/{media.id}")
    assert jellyfin.asked == 1


def test_without_jellyfin_the_page_still_shows_the_genres_it_knows(
    app, client, monkeypatch
):
    monkeypatch.setattr(
        "curatarr.services.client", lambda _kind: FakeJellyfin(fails=True)
    )
    media, _parts = _show("Quiet", [(1, 1, True)])
    media.genres = ["Drama"]
    db.session.commit()
    page = client.get(f"/titles/{media.id}").text
    assert "Couldn't reach Jellyfin for its summary just now." in page
    assert '<span class="chip">Drama</span>' in page
    assert "Nobody has played it yet." in page


def _played(media, who, at, *, finished=False, position=0, key):
    from curatarr.models import LifecycleAction, LifecycleEvent

    event = LifecycleEvent(
        external_event_id=key,
        source="jellyfin",
        event_type="item_played" if finished else "playback_stopped",
        occurred_at=at,
        media_identity_id=media.id,
        jellyfin_user_id=who,
        payload_hash=key,
        normalized_json={"played": finished, "position_ticks": position * 10_000_000},
    )
    db.session.add(event)
    db.session.flush()
    db.session.add(
        LifecycleAction(
            idempotency_key=f"playback:{key}",
            action_type="playback_received",
            state="SUCCEEDED",
            media_identity_id=media.id,
            event_id=event.id,
            reason_text="Playback reset the inactivity timer.",
        )
    )


def test_a_films_page_says_who_finished_it_and_how_far_they_got(
    app, client, monkeypatch
):
    """Watched over two evenings: the first says where they stopped, the
    second that they finished (its earlier stop folded in), not a row of
    identical "Playback received" lines."""
    from test_enrichment import _movie

    item = {"RunTimeTicks": 120 * 600_000_000, "Overview": "A bodybuilder's story."}
    monkeypatch.setattr("curatarr.services.client", lambda _kind: FakeJellyfin(item))
    set_setting("jellyfin_user_names", {"u1": "Alex"})
    _library, media, _part = _movie()
    # Midday where the server is, so "an hour earlier" is the same day
    # whatever time the tests run.
    noon = utcnow().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday, today = noon - timedelta(days=1), noon
    _played(media, "u1", yesterday, position=37 * 60, key="a")
    _played(media, "u1", today - timedelta(hours=1), position=90 * 60, key="b")
    _played(media, "u1", today, finished=True, key="c")
    db.session.add(
        UserMediaState(
            media_identity_id=media.id,
            jellyfin_user_id="u1",
            last_played_at=today,
            play_count=1,
        )
    )
    db.session.commit()
    page = client.get(f"/titles/{media.id}").text
    watching = page[
        page.index('id="watching-heading"') : page.index('id="origin-heading"')
    ]
    assert "<strong>Alex</strong> · finished it" in watching
    recent = page[page.index("Recent activity") :]
    assert "Alex finished it" in recent and "Alex stopped it at 37:00 (31%)" in recent
    assert "1:30:00" not in recent  # today's stop is folded into the finish
    assert "Playback reset the inactivity timer." not in recent
    # the poster sits in the About card
    about = page[page.index("title-about") : page.index('id="about-heading"')]
    assert (
        "picture-open title-about-poster" in about and f"/posters/{media.id}" in about
    )
