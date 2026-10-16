from test_acquisition_reconcile import _show

from curatarr import db, wrong_version
from curatarr.models import ChapterState, LibraryPolicy

SITCOM = {
    "id": 306,
    "title": "All in the Family",
    "year": 1971,
    "originalLanguage": {"name": "English"},
}


def file(file_id, name, audio="eng"):
    return {"id": file_id, "sceneName": name, "mediaInfo": {"audioLanguages": audio}}


def test_most_episodes_in_another_language_is_another_show():
    files = [
        (file(i, f"All.In.The.Family.S01E0{i}.720p", "chi"), 1971) for i in range(1, 6)
    ]
    verdict = wrong_version.judge(SITCOM, files, {i: "zh" for i in range(1, 6)})
    assert verdict["language"] == "zh" and len(verdict["files"]) == 5
    assert verdict["evidence"][0].startswith(
        "5 of 5 transcribed episodes are in Chinese"
    )


def test_a_dual_audio_release_is_the_right_show():
    """Off Campus: transcribed from the Turkish track of an eng/tur file."""
    files = [
        (file(i, f"Off.Campus.S01E0{i}.DUAL", "eng/tur"), 2026) for i in range(1, 4)
    ]
    show = {**SITCOM, "title": "Off Campus", "year": 2026}
    assert wrong_version.judge(show, files, {i: "tr" for i in range(1, 4)}) is None


def test_a_few_scenes_in_another_language_are_fine():
    files = [(file(i, f"Patriot.S01E0{i}", ""), 2015) for i in range(1, 6)]
    show = {**SITCOM, "title": "Patriot", "year": 2015}
    languages = {1: "fr", 2: "en", 3: "en", 4: "fr", 5: "en"}
    assert wrong_version.judge(show, files, languages) is None


def test_release_names_from_another_year():
    """Good Times 1974 with the 2024 Netflix show's episodes."""
    files = [
        (file(i, f"Good.Times.S01E0{i}.2024.1080p.Netflix.WEB-DL"), 1974)
        for i in range(1, 6)
    ]
    show = {**SITCOM, "title": "Good Times", "year": 1974}
    verdict = wrong_version.judge(show, files, {})
    assert verdict["year"] == 2024 and len(verdict["files"]) == 5
    assert (
        "5 of 5 release names say 2024, but the show is from 1974"
        in verdict["evidence"][0]
    )


def test_years_that_belong():
    show = {**SITCOM, "title": "1923", "year": 2022}
    files = [
        (file(1, "1923.S01E01.1080p"), 2022),  # the title
        (file(2, "Show.2023.S01E02"), 2023),  # the episode's air year
        (file(3, "Show.S01E03.2160p"), 2022),  # a resolution
    ]
    assert wrong_version.judge(show, files, {}) is None


class FakeSonarr:
    def __init__(self, files):
        self.files, self.calls = files, []

    def series(self):
        return [{**SITCOM, "id": 77}]

    def episode_files(self, _series_id):
        return self.files

    def ignore_for_series(self, *args):
        self.calls.append(("ignore", *args))

    def episode_grabs(self, episode_id):
        return [{"id": 900 + episode_id, "downloadId": "pack"}]

    def mark_failed(self, history_id):
        self.calls.append(("blocklist", history_id))

    def delete_episode_file(self, file_id):
        self.calls.append(("delete", file_id))

    def episode_search(self, episode_ids):
        self.calls.append(("search", list(episode_ids)))


def test_sweep_lists_it_and_replace_keeps_it_from_coming_back(app, client, monkeypatch):
    media, parts = _show(
        "All in the Family", [(1, e, True) for e in range(1, 4)], sonarr_id=77
    )
    for e, part in parts.items():
        part.arr_file_id = 500 + e[1]
    # Replacing deletes files, which a library in dry run never does.
    db.session.add(
        LibraryPolicy(library_id=media.library_id, policy_json={"dry_run": False})
    )
    db.session.commit()
    sonarr = FakeSonarr(
        [
            file(500 + e, f"All.In.The.Family.S01E0{e}.1976.720p.MyTVSuper")
            for e in (1, 2, 3)
        ]
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: sonarr)
    assert wrong_version.sweep() == 1 and wrong_version.sweep() == 0  # until it changes
    page = client.get("/chapters/problems").text
    assert "Another show with the same name" in page and "Replace 3 files" in page
    assert "release names say 1976" in client.get("/").text
    page = client.post(
        f"/chapters/problems/version/{media.id}/replace", follow_redirects=True
    ).text
    assert "Sonarr now ignores releases mentioning 1976 for this show" in page
    # The same uploader's other releases can't come back: ignored by year, for this show.
    assert sonarr.calls[0] == (
        "ignore",
        77,
        "curatarr-77",
        r"/\b1976\b/",
        "Curatarr: not the 1976 All in the Family",
    )
    assert ("blocklist", 900 + parts[(1, 1)].sonarr_episode_id) in sonarr.calls
    assert [c for c in sonarr.calls if c[0] == "blocklist"].__len__() == 1  # one pack
    assert {c[1] for c in sonarr.calls if c[0] == "delete"} == {501, 502, 503}
    assert sonarr.calls[-1] == (
        "search",
        sorted(p.sonarr_episode_id for p in parts.values()),
    )
    assert wrong_version.rows() == []


def test_its_right_holds_until_the_evidence_changes(app, client, monkeypatch):
    media, parts = _show(
        "All in the Family", [(1, e, True) for e in range(1, 4)], sonarr_id=77
    )
    for e, part in parts.items():
        part.arr_file_id = 500 + e[1]
        db.session.add(
            ChapterState(
                media_part_id=part.id,
                media_identity_id=media.id,
                talk_stats={"language": "zh"},
            )
        )
    db.session.commit()
    sonarr = FakeSonarr([file(500 + e, f"S01E0{e}", "chi") for e in (1, 2, 3)])
    monkeypatch.setattr("curatarr.services.client", lambda _kind: sonarr)
    wrong_version.sweep()
    client.post(f"/chapters/problems/version/{media.id}/dismiss")
    assert wrong_version.rows() == []
    parts[(1, 3)].arr_file_id = 599  # a new file
    sonarr.files[2] = file(599, "S01E03.1976", "chi")
    db.session.commit()
    wrong_version.sweep()
    assert [r["media"].title for r in wrong_version.rows()] == ["All in the Family"]


def test_ignoring_a_year_is_limited_to_the_one_show(monkeypatch):
    from curatarr.integrations import SonarrClient

    sonarr = SonarrClient("http://sonarr", "key")
    sent = []
    state = {"tags": [], "profiles": [], "series": {"id": 77, "tags": [3]}}

    def request(method, path, json=None, **_):
        sent.append((method, path, json))
        if (method, path) == ("GET", "/api/v3/tag"):
            return state["tags"]
        if (method, path) == ("POST", "/api/v3/tag"):
            return {"id": 9, "label": json["label"]}
        if (method, path) == ("GET", "/api/v3/releaseprofile"):
            return state["profiles"]
        if method == "GET":
            return dict(state["series"])
        return json

    monkeypatch.setattr(sonarr, "request", request)
    sonarr.ignore_for_series(77, "curatarr-77", r"/\b1976\b/", "Not 1976")
    profile = next(
        j for m, p, j in sent if (m, p) == ("POST", "/api/v3/releaseprofile")
    )
    assert profile["ignored"] == [r"/\b1976\b/"] and profile["tags"] == [9]
    series = next(j for m, p, j in sent if (m, p) == ("PUT", "/api/v3/series/77"))
    assert series["tags"] == [3, 9]  # its other tags kept
