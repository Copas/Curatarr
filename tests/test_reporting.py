from curatarr import db
from curatarr.demo import seed_demo
from curatarr.models import MediaIdentity, PurgeCandidate, utcnow


def test_demo_exposes_review_leaving_soon_policy_and_history(app, client):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        seed_demo()
        review = db.session.query(PurgeCandidate).filter_by(state="REVIEW").one()
        media_id = review.media_identity_id
        leaving = db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").one()
        leaving_title = db.session.get(MediaIdentity, leaving.media_identity_id).title
    overview = client.get("/")
    assert overview.status_code == 200
    assert b"High" in overview.data and b"Low" in overview.data
    assert b"Recent decisions" in overview.data
    queue = client.get("/review")
    assert queue.status_code == 200
    assert b"Demo Movie 07" in queue.data
    assert leaving_title.encode() in queue.data
    assert b"Last watched" in queue.data
    title = client.get(f"/titles/{media_id}")
    assert title.status_code == 200
    assert b"Lifecycle decisions" in title.data
    assert b"Effective policy" in title.data
    assert b"Decision score and observations" in title.data
    assert client.get("/history").status_code == 200


def test_title_shows_current_cleanup_status_before_controls(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    review = db.session.query(PurgeCandidate).filter_by(state="REVIEW").one()
    leaving = db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").one()

    page = client.get(f"/titles/{review.media_identity_id}").text
    assert "Waiting for a cleanup decision." in page
    assert page.index("Cleanup status") < page.index("Lifecycle decisions")

    page = client.get(f"/titles/{leaving.media_identity_id}").text
    assert "Leaving Soon: scheduled for" in page
    assert "See the decision details" in page

    client.post(f"/review/{review.id}/snooze_30")
    page = client.get(f"/titles/{review.media_identity_id}").text
    assert "Cleanup is snoozed until" in page

    # Only some episodes leaving: the show isn't.
    leaving.target_part_ids = ["one", "two"]
    db.session.commit()
    page = client.get(f"/titles/{leaving.media_identity_id}").text
    assert "2 episodes up for cleanup, scheduled for" in page
    assert "The show itself stays." in page
    assert "Leaving Soon: scheduled for" not in page

    protected = db.session.query(MediaIdentity).filter_by(title="Demo Series 04").one()
    page = client.get(f"/titles/{protected.id}").text
    assert "Protected: Never Purge is on for this title." in page


def test_demo_outage_is_visible_in_integration_health(app, client):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        seed_demo()
    assert (
        client.post("/demo/simulate", data={"choice": "outage_radarr"}).status_code
        == 302
    )
    status = client.get("/api/v1/status").json
    assert status["integrations"]["radarr"] == "unhealthy (simulated)"
    assert b"unhealthy (simulated)" in client.get("/").data


def test_history_filters_by_title_library_user_and_date(app, client):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        seed_demo()
        library_id = (
            db.session.query(MediaIdentity)
            .filter_by(jellyfin_id="demo-series-1")
            .one()
            .library_id
        )
    client.post("/demo/simulate", data={"choice": "complete_episode"})
    day = utcnow().date().isoformat()
    response = client.get(
        "/history",
        query_string={
            "q": "Demo Series 01",
            "library": library_id,
            "user": "demo-viewer-a",
            "from": day,
            "to": day,
        },
    )
    assert response.status_code == 200
    assert b"Playback Received" in response.data
    assert b"Demo Series 01" in response.data
    assert client.get("/history", query_string={"from": "bad-date"}).status_code == 400


def test_history_filter_choices_come_from_recorded_values(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    client.post("/demo/simulate", data={"choice": "complete_episode"})
    page = client.get("/history").text
    assert '<option value="demo-viewer-a" >Avery</option>' in page
    assert '<option value="sonarr_season_search" >Sonarr Season Search</option>' in page
    assert client.get("/history?from=not-a-date").status_code == 400
    assert client.get("/history/unknown").status_code == 404


def test_overview_buttons_work_in_place_but_long_jobs_reload(client):
    page = client.get("/").text
    assert "data-live-page" in page and "live-page.js" in page
    # Reconcile now starts something the page follows from a fresh load.
    assert 'action="/reconcile" data-reload' in page
