from datetime import timedelta

from curatarr import db
from curatarr.demo import seed_demo
from curatarr.lifecycle import expire_notices
from curatarr.models import Integration, Library, LibraryPolicy, PurgeCandidate, utcnow
from curatarr.services import set_setting, setup_progress


def _done():
    return {step["key"]: step["done"] for step in setup_progress()}


def test_setup_steps_follow_stored_state(app, client):
    assert not any(_done().values())
    page = client.get("/")
    assert b"Continue setup" in page.data
    for kind in ("jellyfin", "sonarr", "radarr"):
        db.session.add(
            Integration(
                kind=kind,
                base_url=f"http://{kind}.local",
                secret_ref="encrypted",
                health_state="healthy" if kind != "radarr" else "unhealthy",
            )
        )
    set_setting("webhook_token", "token")
    library = Library(jellyfin_library_id="l", name="Movies", media_type="movies")
    db.session.add(library)
    db.session.commit()
    done = _done()
    assert done["jellyfin"] and done["sonarr"] and not done["radarr"]
    assert done["webhook"] and done["discover"] and not done["policy"]
    assert b"Continue setup" in client.get("/").data
    db.session.query(Integration).filter_by(
        kind="radarr"
    ).one().health_state = "healthy"
    db.session.commit()
    # Reviewing rules is optional, so setup is complete without it.
    assert b"Continue setup" not in client.get("/").data
    db.session.add(LibraryPolicy(library_id=library.id, policy_json={"dry_run": False}))
    db.session.commit()
    assert _done()["policy"]
    setup_page = client.get("/setup")
    assert setup_page.status_code == 200
    assert b"Deletion is enabled for: Movies" in setup_page.data


def test_overview_lists_scheduled_and_recent_cleanup(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    page = client.get("/")
    assert b"Continue setup" not in page.data
    assert b"Demo Series 05" in page.data
    assert b"No cleanup has run yet." in page.data
    candidate = db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").one()
    candidate.scheduled_delete_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert expire_notices() == 1
    page = client.get("/")
    assert b"Dry run (not deleted)" in page.data
    assert b"No deletions are scheduled." in page.data


def test_unmanaged_libraries_are_named_not_shown_as_empty(app, client):
    for name, media_type in (
        ("Shows", "tv"),
        ("Curated Adult", "other"),
        ("Collections", "other"),
    ):
        db.session.add(
            Library(jellyfin_library_id=name, name=name, media_type=media_type)
        )
    db.session.commit()
    page = client.get("/").text
    assert "Not managed: Collections, Curated Adult." in page
    assert '<h3 class="h5">Curated Adult</h3>' not in page
    assert '<h3 class="h5">Shows</h3>' in page
    rules = client.get("/rules/retention").text
    assert ">Shows</a>" in rules and ">Curated Adult</a>" not in rules
    assert "Dry run is on for every library." in page
    status = client.get("/api/v1/status").get_json()
    assert status["dry_run_libraries"] == ["Shows"]
