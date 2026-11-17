"""What every page tells the browser about where it may load things from."""

import re


def test_pages_load_nothing_from_other_sites(client):
    response = client.get("/login")
    policy = response.headers["Content-Security-Policy"]
    # Same site only: no CDN, so pages look right without the internet.
    assert "http" not in policy
    assert "default-src 'self'" in policy
    html = response.get_data(as_text=True)
    sources = re.findall(r'(?:href|src)="(https?://[^"]+)"', html)
    assert not [s for s in sources if not s.startswith("http://localhost")], sources
    stylesheet = client.get("/static/bootstrap.min.css")
    assert stylesheet.status_code == 200
    assert b"Bootstrap  v5.3.3" in stylesheet.data


def _swap_check():
    """The inline-script check in static/swap.js, as a Python pattern."""
    from pathlib import Path

    source = Path(__file__).parents[1].joinpath("curatarr/static/swap.js").read_text()
    pattern = re.search(r"var SCRIPTED = /(.+)/i;", source).group(1)
    return re.compile(pattern.replace("\\/", "/"), re.IGNORECASE)


def test_pages_pass_the_inline_script_check(app, client):
    """Pages redrawn in place go through swap.js's check, which refuses HTML
    with inline script. None of Curatarr's own pages may trip it, or a
    button that works in place would fall back to reloading the page."""
    check = _swap_check()
    assert check.search('<a href="javascript:alert(1)">')
    assert check.search('<img src="x" onerror="alert(1)">')
    assert not check.search('<p>Turn it on = off</p><a href="/x">on</a>')
    looked = 0
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments or rule.endpoint == "static":
            continue
        response = client.get(rule.rule)
        if response.status_code != 200 or response.mimetype != "text/html":
            continue
        looked += 1
        html = response.get_data(as_text=True)
        found = check.search(html)
        assert not found, (
            f"{rule.rule}: {html[max(found.start() - 80, 0) : found.end() + 40]}"
        )
    assert looked > 20


def test_templates_have_no_inline_styles():
    """The security policy refuses style="" attributes (no 'unsafe-inline'),
    so one would silently do nothing: sizes go in style.css as classes."""
    from pathlib import Path

    templates = Path(__file__).parents[1] / "curatarr" / "templates"
    found = [
        f"{path.name}: {match.group(0)}"
        for path in templates.rglob("*.html")
        for match in re.finditer(r'\sstyle="[^"]*"', path.read_text())
    ]
    assert not found, found


def test_other_sites_can_link_but_not_post(client):
    page = client.get("/titles")
    assert (
        "require-trusted-types-for 'script'" in page.headers["Content-Security-Policy"]
    )
    assert page.headers["Cross-Origin-Opener-Policy"] == "same-origin-allow-popups"
    assert page.headers["Cross-Origin-Resource-Policy"] == "same-site"
    assert "Sec-Fetch-Site" in page.headers["Vary"]
    elsewhere = {"Sec-Fetch-Site": "cross-site"}
    # a link from another site opens the page…
    link = elsewhere | {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert client.get("/titles", headers=link).status_code == 200
    # …but its forms, fetches and pictures are turned away
    form = elsewhere | {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert client.post("/preferences/theme", headers=form).status_code == 403
    fetched = elsewhere | {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    assert client.get("/titles", headers=fetched).status_code == 403
    assert client.get("/static/logo.svg", headers=elsewhere).status_code == 403
    # the same machine, a bookmark, and callers that don't say (Jellyfin's
    # plugin) are let through
    for site in ("same-origin", "same-site", "none"):
        assert (
            client.get("/titles", headers={"Sec-Fetch-Site": site}).status_code == 200
        )
    assert client.get("/titles").status_code == 200


def test_bootstrap_system_css_is_generated_from_bootstrap():
    """static/bootstrap-system.css (Bootstrap's dark colours for a page that
    follows the device) is what scripts/bootstrap_system_theme.py writes
    from the Bootstrap that ships, so the two never drift apart."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "bootstrap_system_theme", root / "scripts" / "bootstrap_system_theme.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    static = root / "curatarr" / "static"
    written = module.system_css((static / "bootstrap.min.css").read_text())
    assert (static / "bootstrap-system.css").read_text() == written
    assert ":root:not([data-bs-theme]){color-scheme:dark;" in written


def test_pages_fetch_links_early_and_a_post_clears_them(app):
    from curatarr.routes import SPECULATION_RULES

    client = app.test_client()
    app.config["ALLOW_UNAUTHENTICATED"] = False
    page = client.get("/login")
    assert page.headers["Speculation-Rules"] == '"/speculation-rules.json"'
    rules = client.get("/speculation-rules.json")  # signed out too
    assert rules.status_code == 200
    assert rules.mimetype == "application/speculationrules+json"
    assert rules.get_json() == SPECULATION_RULES
    # only fetched, and only once a link is pointed at
    (rule,) = rules.get_json()["prefetch"]
    assert "prerender" not in rules.get_json() and rule["eagerness"] == "moderate"
    assert "Speculation-Rules" not in client.get("/static/logo.svg").headers
    # anything posted clears what was fetched early
    posted = client.post("/preferences/theme", data={"theme": "dark"})
    assert posted.headers["Clear-Site-Data"] == '"prefetchCache"'
    assert "Clear-Site-Data" not in page.headers
