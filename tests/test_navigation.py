import re

from curatarr import db
from curatarr.demo import seed_demo
from curatarr.models import LifecycleAction, MediaIdentity


def _active(page):
    return re.findall(
        r'class="app-nav-link active" href="[^"]*" aria-current="page">(\w+)<', page
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
        "/rules/acquisition": "Manage",
        f"/rules/acquisition?scope={library.id}": "Manage",
        "/rules/retention": "Manage",
        f"/libraries/{library.id}/policy": "Manage",
        "/review": "Review",
        "/titles": "Library",
        f"/titles/{media.id}": "Library",
        "/history": "Activity",
        f"/history/{action.id}": "Activity",
        "/settings": "Manage",
        "/setup": "Manage",
        "/operations": "Manage",
    }
    for url, label in expected.items():
        assert _active(client.get(url).text) == [label], url


def test_library_browse_filters_and_paginates(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    first = client.get("/titles").text
    assert "Browse titles" in first
    assert "70 titles" in first
    assert "Next" in first
    assert "Demo Movie 01" in first
    second = client.get("/titles?page=2").text
    assert "Previous" in second
    movies = client.get("/titles?type=movie&q=Demo+Movie+07").text
    assert "Demo Movie 07" in movies and "1 title" in movies
    assert client.get("/titles?type=unknown").status_code == 400
