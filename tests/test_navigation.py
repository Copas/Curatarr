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
        "/rules/acquisition": "Settings",
        f"/rules/acquisition?scope={library.id}": "Settings",
        "/rules/retention": "Settings",
        f"/libraries/{library.id}/policy": "Settings",
        "/review": "Review",
        "/titles": "Library",
        f"/titles/{media.id}": "Library",
        "/history": "Activity",
        f"/history/{action.id}": "Activity",
        "/settings": "Settings",
        "/setup": "Settings",
        "/operations": "Settings",
        "/chapters/settings": "Settings",
        "/chapters": "Enhance",
        "/chapters/files": "Enhance",
        "/chapters/markers": "Enhance",
        "/chapters/dubs": "Enhance",
        "/search?q=demo": "Library",
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


def test_phone_tab_bar_and_more_page(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    page = client.get("/review").text
    tabs = re.findall(r'class="app-tab( active)?" href="([^"]+)"', page)
    assert [href for _active, href in tabs] == [
        "/",
        "/review",
        "/titles",
        "/chapters",
        "/more",
    ]
    assert [href for active, href in tabs if active] == ["/review"]
    more = client.get("/more").text
    for href in ("/history", "/settings", "/chapters/settings", "/operations"):
        assert f'href="{href}"' in more
    # Activity and Settings live under More on a phone.
    assert 'class="app-tab active" href="/more"' in client.get("/history").text


def test_search_finds_titles_and_says_when_nothing_matches(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    page = client.get("/search?q=Demo+Movie+07").text
    assert "Demo Movie 07" in page and ">Titles<" in page
    assert "No matches" in client.get("/search?q=zzzz-nothing").text


def test_overview_lists_what_needs_someone(app, client):
    app.config["DEMO_MODE"] = True
    seed_demo()
    client.post("/reconcile")
    from curatarr.models import PurgeCandidate

    leaving = db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").first()
    waiting = db.session.query(PurgeCandidate).filter_by(state="REVIEW").count()
    assert leaving and waiting  # the demo library has both
    page = client.get("/").text
    needs = page.split('id="needs-heading"')[1].split("</section>")[0]
    # Leaving Soon is decided: a card of its own, not something that needs you
    # (under the list it read as part of the row above).
    assert f"/review/{leaving.id}/keep" not in needs and "Leaving soon" not in needs
    card = page.split('id="leaving-heading"')[1].split("</section>")[0]
    assert "unless someone watches" in card
    assert f"{waiting} titles waiting for a decision" in needs
    response = client.post(f"/review/{leaving.id}/keep", data={"back": "overview"})
    assert response.headers["Location"] == "/"
