"""Jellyfin lists some episodes without a season or episode number; the pages
and the rules that sort or label episodes must take them in their stride."""

from curatarr import db
from curatarr.models import Library, MediaIdentity, MediaPart
from curatarr.policy import DEFAULTS, retained
from curatarr.services import episode_label, episode_order, reconcile_acquisition


def _show():
    library = Library(jellyfin_library_id="tv", name="Shows", media_type="tv")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title="Odd Numbers",
        jellyfin_id="show-1",
        sonarr_id=7,
        arr_monitored=True,
    )
    db.session.add(media)
    db.session.flush()
    numbers = [(1, 1), (1, 2), (None, 5), (1, None), (2, 1)]
    for season, episode in numbers:
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=season,
                episode_number=episode,
                has_file=True,
                size_bytes=100,
            )
        )
    db.session.commit()
    return media


def test_a_show_with_unnumbered_episodes_still_has_a_title_page(app, client):
    media = _show()
    page = client.get(f"/titles/{media.id}")
    assert page.status_code == 200 and "Odd Numbers" in page.text
    assert "Season 1" in page.text and "Season None" not in page.text


def test_unnumbered_episodes_sort_and_label_without_breaking(app):
    media = _show()
    parts = sorted(media.parts, key=episode_order)
    assert [(p.season_number, p.episode_number) for p in parts][:2] == [
        (None, 5),
        (1, None),
    ]
    assert episode_label(None, 5) == "S?E05" and episode_label(1, None) == "S01E?"
    policy = DEFAULTS | {"minimum_episodes": 3}
    assert not retained(
        next(p for p in media.parts if p.episode_number is None), policy
    )
    reconcile_acquisition()  # the hourly always-keep fill reads every episode


def test_the_activity_panel_takes_a_subtitle_check_with_no_status_yet(app, client):
    """Read while another job was part way through updating it, a finished
    subtitle check had no status, and the live panel crashed."""
    from curatarr.models import ChapterState, utcnow

    media = _show()
    part = media.parts[0]
    db.session.add(
        ChapterState(
            media_part_id=part.id,
            media_identity_id=media.id,
            subtitle_processed_at=utcnow(),
            subtitle_status=None,
        )
    )
    db.session.commit()
    page = client.get("/chapters/activity")
    assert page.status_code == 200
