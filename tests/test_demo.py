from curatarr import db
from curatarr.demo import seed_demo
from curatarr.models import Library, MediaIdentity


def test_demo_seed_and_simulation(app):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        assert seed_demo()
        assert not seed_demo()
        assert db.session.query(Library).count() == 3
        assert db.session.query(MediaIdentity).count() == 70
    client = app.test_client()
    assert client.get("/").status_code == 200
    response = client.post("/demo/simulate", data={"choice": "complete_episode"})
    assert response.status_code == 302
    assert (
        client.post("/demo/simulate", data={"choice": "add_movie"}).status_code == 302
    )
    with app.app_context():
        from curatarr.models import LifecycleAction

        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="sonarr_season_search")
            .count()
            >= 1
        )
        action_id = db.session.query(LifecycleAction).first().id
        assert db.session.query(MediaIdentity).count() == 71
    assert client.get(f"/history/{action_id}").status_code == 200


def test_every_demo_control_applies(app):
    from curatarr.demo import SIMULATIONS
    from curatarr.models import MediaPart
    from curatarr.services import setting

    app.config["DEMO_MODE"] = True
    client = app.test_client()
    for choice in sorted(SIMULATIONS):
        response = client.post("/demo/simulate", data={"choice": choice})
        assert response.status_code == 302, choice
        if choice.startswith("outage_"):
            assert setting("demo_outage") == choice.split("_")[1]
    assert setting("demo_outage") is None  # restore_integrations followed the outages.
    assert client.post("/demo/simulate", data={"choice": "nope"}).status_code == 400
    sizes = {
        part.size_bytes
        for part in db.session.query(MediaPart).filter_by(kind="movie_file")
    }
    assert 3_000_000_000 in sizes
    app.config["DEMO_MODE"] = False
    assert (
        client.post("/demo/simulate", data={"choice": "add_movie"}).status_code == 404
    )
