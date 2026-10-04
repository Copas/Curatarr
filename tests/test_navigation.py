import re

from curatarr import db
from curatarr.demo import seed_demo
from curatarr.models import LifecycleAction, MediaIdentity


def _active(page):
    return re.findall(
        r'class="nav-link active" href="[^"]*" aria-current="page">(\w+)<', page
    )


def test_navigation_marks_the_current_section(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    client.post("/demo/simulate", data={"choice": "complete_episode"})
    media = db.session.query(MediaIdentity).first()
    action = db.session.query(LifecycleAction).first()
    library = media.library
    expected = {
        "/": "Overview",
        "/rules/acquisition": "Acquisition",
        f"/rules/acquisition?scope={library.id}": "Acquisition",
        "/rules/retention": "Retention",
        f"/libraries/{library.id}/policy": "Retention",
        "/review": "Review",
        "/titles": "Overrides",
        f"/titles/{media.id}": "Overrides",
        "/history": "History",
        f"/history/{action.id}": "History",
        "/settings": "Settings",
        "/setup": "Settings",
    }
    for url, label in expected.items():
        assert _active(client.get(url).text) == [label], url
