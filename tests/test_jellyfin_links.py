"""Every title Curatarr shows links to the same item in Jellyfin."""

from curatarr import db
from curatarr.models import Integration, Library, MediaIdentity, MediaPart
from curatarr.services import set_setting


def _setup(base_url="http://127.0.0.1:8096"):
    db.session.add(Integration(kind="jellyfin", base_url=base_url))
    library = Library(jellyfin_library_id="lib-tv", name="Shows", media_type="tv")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title="Cardinal",
        jellyfin_id="abc123",
    )
    db.session.add(media)
    db.session.flush()
    part = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=1,
        jellyfin_id="ep456",
        has_file=True,
        file_path="/tv/Cardinal/S01E01.mkv",
    )
    db.session.add(part)
    db.session.commit()
    return media, part


def test_loopback_jellyfin_links_use_the_host_the_page_was_opened_on(app, client):
    media, _part = _setup()
    set_setting("jellyfin_server_id", "srv1")
    page = client.get(
        f"/titles/{media.id}", base_url="http://192.0.2.10:8787"
    ).get_data(as_text=True)
    assert "http://192.0.2.10:8096/web/#/details?id=abc123&amp;serverId=srv1" in page
    assert "Open in Jellyfin" in page and "127.0.0.1:8096" not in page


def test_a_named_jellyfin_host_is_used_as_configured(app, client):
    media, _part = _setup("https://jellyfin.example.test")
    page = client.get(f"/titles/{media.id}").get_data(as_text=True)
    assert "https://jellyfin.example.test/web/#/details?id=abc123" in page


def test_lists_and_chapter_pages_link_titles_and_episodes(app, client):
    media, part = _setup()
    assert 'title="Open Cardinal in Jellyfin"' in client.get("/titles").get_data(
        as_text=True
    )
    page = client.get(f"/chapters/parts/{part.id}").get_data(as_text=True)
    assert "details?id=ep456" in page
    title = client.get(f"/titles/{media.id}").get_data(as_text=True)
    assert 'title="Open Cardinal S01E01 in Jellyfin"' in title


def test_no_link_without_a_jellyfin_integration(app, client):
    library = Library(jellyfin_library_id="lib", name="Shows", media_type="tv")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id, media_type="series", title="X", jellyfin_id="id"
    )
    db.session.add(media)
    db.session.commit()
    page = client.get(f"/titles/{media.id}").get_data(as_text=True)
    assert "Open in Jellyfin" not in page and "jf-link" not in page


def test_newer_pages_link_every_title_too(app, client):
    """Fun facts, search, Overview's Needs you and Progress's activity."""
    from datetime import timedelta

    from curatarr import trivia
    from curatarr.models import ChapterState, utcnow

    media, part = _setup()
    media.sonarr_id, media.arr_monitored = 9, False
    part.has_file = False  # an always-keep episode that never arrived
    part.sonarr_episode_id, part.air_date = 99, utcnow() - timedelta(days=400)
    state = ChapterState(
        media_part_id=part.id,
        media_identity_id=media.id,
        job_state="queued",
        next_attempt_at=utcnow() + timedelta(minutes=10),
        chapter_error="Waiting: the file can't be reached right now",
    )
    db.session.add(state)
    db.session.commit()
    link = "details?id=abc123"
    assert link in client.get("/").get_data(as_text=True)  # Needs you
    assert link in client.get("/search?q=Cardinal").get_data(as_text=True)
    assert "details?id=ep456" in client.get("/chapters").get_data(as_text=True)
    trivia._cache["facts"] = (
        1e18,
        {
            "totals": {
                "files": 1,
                "minutes": 60,
                "words": 10,
                "swears": 0,
                "f_words": 0,
                "love": 0,
                "panic": 0,
            },
            "films": 0,
            "episodes": 1,
            "categories": [],
            "catchphrases": [
                {"media": media, "phrase": "never gonna", "episodes": 3, "of": 4}
            ],
        },
    )
    try:
        assert link in client.get("/fun").get_data(as_text=True)
    finally:
        trivia._cache.clear()
