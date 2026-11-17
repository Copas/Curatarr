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
