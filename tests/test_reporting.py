from curatarr import db
from curatarr.demo import seed_demo
from curatarr.models import MediaIdentity, PurgeCandidate


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
