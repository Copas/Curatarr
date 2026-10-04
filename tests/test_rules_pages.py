from curatarr import db
from curatarr.models import Library, LibraryPolicy
from curatarr.services import resolved_policy, setting


def _library(policy=None):
    library = Library(jellyfin_library_id="rules", name="Rules TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    if policy is not None:
        db.session.add(LibraryPolicy(library_id=library.id, policy_json=policy))
    db.session.commit()
    return library


def test_rule_pages_render_for_global_and_library(app, client):
    library = _library()
    for section in ("acquisition", "retention"):
        for scope in ("global", library.id):
            page = client.get(f"/rules/{section}?scope={scope}")
            assert page.status_code == 200
    assert b"Keep one season ahead" in client.get("/rules/acquisition").data
    assert client.get("/rules/unknown").status_code == 404
    assert client.get("/rules/retention?scope=missing").status_code == 404


def test_global_rules_are_inherited_by_libraries(app, client):
    library = _library()
    client.post(
        "/rules/acquisition", data={"grace_days": "7", "manage_specials": "true"}
    )
    assert setting("global_policy") == {"grace_days": 7, "manage_specials": True}
    page = client.get(f"/rules/acquisition?scope={library.id}").text
    assert 'placeholder="Inherit (7)"' in page
    assert 'placeholder="Inherit (3)"' in page
    assert "Currently in effect: 7 (global default)" in page
    page = client.get("/rules/acquisition").text
    # Global values have nothing to inherit from, so the page shows real values.
    assert "Inherit" not in page and "Built-in default" not in page
    assert 'name="grace_days" value="7"' in page
    assert 'name="minimum_episodes" value="3"' in page
    assert '<option value="true" selected>Yes</option>' in page


def test_partial_page_keeps_other_library_fields(app, client):
    library = _library(
        {"quota_enabled": True, "high_water_bytes": 200, "low_water_bytes": 100}
    )
    client.post(
        f"/rules/acquisition?scope={library.id}",
        data={"minimum_mode": "season_1", "grace_days": ""},
    )
    db.session.refresh(library.policy)
    assert library.policy.policy_json == {
        "quota_enabled": True,
        "high_water_bytes": 200,
        "low_water_bytes": 100,
        "minimum_mode": "season_1",
    }
    client.post(f"/rules/acquisition?scope={library.id}", data={"minimum_mode": ""})
    db.session.refresh(library.policy)
    assert "minimum_mode" not in library.policy.policy_json


def test_global_defaults_cover_size_space_and_dry_run(app, client, tmp_path):
    shows = _library()
    page = client.get("/rules/retention").text
    for field in ("dry_run", "quota_enabled", "free_space_enabled", "disk_path"):
        assert f'name="{field}"' in page
    # Every library is in dry run by default.
    assert "Dry run is on for every library." in client.get("/").text
    client.post(
        "/rules/retention",
        data={
            "dry_run": "false",
            "free_space_enabled": "true",
            "disk_path": str(tmp_path),
            "low_free_percent": "15",
        },
    )
    assert setting("global_policy") == {
        "dry_run": False,
        "free_space_enabled": True,
        "disk_path": str(tmp_path),
    }
    library_page = client.get(f"/rules/retention?scope={shows.id}").text
    assert "Currently in effect: No (global default)" in library_page
    assert "Deletion is on for every library." in client.get("/").text
    # A library can still keep itself in dry run.
    client.post(f"/rules/retention?scope={shows.id}", data={"dry_run": "true"})
    movies = Library(jellyfin_library_id="m", name="Films", media_type="movies")
    db.session.add(movies)
    db.session.commit()
    banner = client.get("/").text
    assert "<strong>Deletion is on for:</strong> Films." in banner
    assert "<strong>Dry run (nothing is removed) for:</strong> Rules TV." in banner
    status = client.get("/api/v1/status").get_json()
    assert status["dry_run"] is False
    assert status["deleting_libraries"] == ["Films"]


def test_global_change_that_breaks_a_library_is_rejected(app, client):
    library = _library({"low_free_percent": 10})
    response = client.post(
        "/rules/retention", data={"critical_free_percent": "12"}, follow_redirects=True
    )
    assert b"Rules TV: Critical free-space threshold must be below low" in response.data
    assert setting("global_policy", {}) == {}
    db.session.refresh(library)
    from curatarr.models import MediaIdentity

    media = MediaIdentity(
        library_id=library.id, media_type="series", title="Show", jellyfin_id="s"
    )
    db.session.add(media)
    db.session.commit()
    assert resolved_policy(media)[0]["critical_free_percent"] == 8


def test_invalid_number_is_reported(app, client):
    response = client.post(
        "/rules/acquisition", data={"grace_days": "soon"}, follow_redirects=True
    )
    assert b"Invalid grace_days" in response.data
    assert setting("global_policy", {}) == {}


def test_minimum_footprint_options_explain_themselves(app, client):
    page = client.get("/rules/acquisition").text
    assert "Always keep for each show" in page
    for label in (
        "First N episodes of Season 1",
        "All of Season 1",
        "Entire series (never trimmed)",
    ):
        assert f">{label}</option>" in page
    assert '<option value="first_n_episodes" selected>' in page


def test_yes_no_dropdowns_match_the_default_wording(app, client):
    import re

    library = _library()
    page = client.get(f"/libraries/{library.id}/policy").text
    for name in (
        "keep_one_season_ahead",
        "manage_specials",
        "quota_enabled",
        "dry_run",
    ):
        select = re.search(rf'name="{name}">(.*?)</select>', page, re.DOTALL).group(1)
        options = re.findall(r">([^<]+)</option>", select)
        # Same words as the "(Yes)"/"(No)" default label, always Yes first.
        assert options[1:] == ["Yes", "No"], (name, options)


def test_saving_the_global_page_stores_only_changes(app, client):
    from curatarr.policy import DEFAULTS

    # A browser submits every shown value; only differences are kept.
    form = {
        "minimum_mode": DEFAULTS["minimum_mode"],
        "minimum_episodes": str(DEFAULTS["minimum_episodes"]),
        "acquisition_threshold": "2",
        "keep_one_season_ahead": "true",
        "manage_specials": "false",
        "grace_days": str(DEFAULTS["grace_days"]),
    }
    client.post("/rules/acquisition", data=form)
    assert setting("global_policy") == {"acquisition_threshold": 2}


def test_global_page_keeps_blank_choices_that_mean_something(app, client):
    page = client.get("/rules/retention").text
    assert '<option value="">Depends on library type' in page
    assert '<option value="">Same as review mode</option>' in page


def test_size_limits_use_mb_gb_tb_with_tb_default(app, client):
    from curatarr.policy import SIZE_UNITS

    library = _library()
    page = client.get(f"/rules/retention?scope={library.id}").text
    assert "Size limit: start cleanup above" in page
    assert "<option selected>TB</option>" in page  # default unit
    client.post(
        f"/rules/retention?scope={library.id}",
        data={
            "quota_enabled": "true",
            "high_water_bytes": "2.5",
            "high_water_bytes_unit": "TB",
            "low_water_bytes": "1800",
            "low_water_bytes_unit": "GB",
        },
    )
    db.session.refresh(library.policy)
    stored = library.policy.policy_json
    assert stored["high_water_bytes"] == round(2.5 * SIZE_UNITS["TB"])
    assert stored["low_water_bytes"] == 1800 * SIZE_UNITS["GB"]
    page = client.get(f"/rules/retention?scope={library.id}").text
    assert 'name="high_water_bytes" value="2.5"' in page
    # 1800 GB displays as 1.76 TB, the largest unit that is at least 1.
    assert 'name="low_water_bytes" value="1.758"' in page
    assert "Currently in effect: 2.50 TB (this library)" in page


def test_bad_sizes_are_rejected(app, client):
    library = _library()
    for value, unit in (("-1", "TB"), ("lots", "TB"), ("1", "PB"), ("nan", "GB")):
        response = client.post(
            f"/rules/retention?scope={library.id}",
            data={"high_water_bytes": value, "high_water_bytes_unit": unit},
            follow_redirects=True,
        )
        assert b"Invalid high_water_bytes" in response.data, (value, unit)


def test_library_inherits_a_global_size_limit(app, client):
    library = _library()
    client.post(
        "/rules/retention",
        data={
            "quota_enabled": "true",
            "high_water_bytes": "3",
            "high_water_bytes_unit": "TB",
            "low_water_bytes": "2",
            "low_water_bytes_unit": "TB",
        },
    )
    page = client.get(f"/rules/retention?scope={library.id}").text
    assert 'placeholder="Inherit (3.00 TB)"' in page
