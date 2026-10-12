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
