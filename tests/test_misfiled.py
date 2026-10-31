"""Episodes Sonarr filed under the wrong episode: the download's name and
what's said in the file must agree before anything is moved."""

from datetime import timedelta

from curatarr import autofix, db, misfiled
from curatarr.models import Library, MediaIdentity, utcnow
from curatarr.services import set_setting, setting

TOPICS = {
    1: "Pilot. Rangers rescue a lighthouse keeper during a hurricane storm",
    2: "Bakery. A pastry chef bakes croissants for a royal wedding banquet",
    3: "Volcano. Geologists climb an erupting volcano sampling molten lava",
    4: "Chess. Grandmasters compete in a chess tournament endgame blunder",
    5: "Submarine. Sailors repair a leaking submarine torpedo hatch underwater",
}


def _episode(number, file_id=None):
    title, overview = TOPICS[number].split(". ", 1)
    return {
        "id": 100 + number,
        "seasonNumber": 1,
        "episodeNumber": number,
        "title": title,
        "overview": overview,
        "hasFile": bool(file_id),
        "episodeFileId": file_id or 0,
        "monitored": True,
    }


class FakeSonarr:
    def __init__(self, episodes, imports, files):
        self._episodes = {e["id"]: e for e in episodes}
        self._imports = imports
        self._files = files
        self.calls = []

    def series(self):
        return [
            {"id": 7, "title": "Show", "path": "/tv/Show", "useSceneNumbering": False}
        ]

    def imports_since(self, _since):
        return self._imports

    def episodes(self, _series_id):
        return list(self._episodes.values())

    def episode(self, episode_id):
        return self._episodes[episode_id]

    def episode_files(self, _series_id):
        return list(self._files.values())

    def episode_file(self, file_id):
        return self._files[file_id]

    def delete_episode_file(self, file_id):
        self.calls.append(("delete", file_id))

    def command(self, name, wait_seconds=0, **fields):
        self.calls.append((name, fields))
        if name == "ManualImport":
            path = fields["files"][0]["path"]
            file_id = next(i for i, f in self._files.items() if f["path"] == path)
            for episode in self._episodes.values():
                if episode["episodeFileId"] == file_id:
                    episode.update(hasFile=False, episodeFileId=0)
            target = self._episodes[fields["files"][0]["episodeIds"][0]]
            target.update(hasFile=True, episodeFileId=file_id)
        return {"id": 1, "status": "completed"}

    def rename_candidates(self, _series_id):
        return []

    def episode_search(self, ids):
        self.calls.append(("search", list(ids)))

    def episode_grabs(self, episode_id):
        return [{"id": 900, "sourceTitle": "Show.S01E02.Volcano.1080p.WEB"}]

    def mark_failed(self, history_id):
        self.calls.append(("blocklist", history_id))

    def set_episodes_monitored(self, ids, monitored):
        self.calls.append(("monitor", list(ids), monitored))


def _imported(episode_id, file_id, name):
    return {
        "seriesId": 7,
        "episode": {"id": episode_id, "title": TOPICS[episode_id - 100].split(".")[0]},
        "data": {
            "droppedPath": f"/downloads/{name}/{name}.mkv",
            "fileId": str(file_id),
        },
    }


def _show():
    library = Library(jellyfin_library_id="tv", name="TV", media_type="tvshows")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id, media_type="series", title="Show", sonarr_id=7
    )
    db.session.add(media)
    db.session.commit()
    return media


def _said(topics):
    return lambda episode_id: topics.get(episode_id)


def test_release_names():
    name = "Show.S01E02.Volcano.1080p.WEB-DL.DDP5.1.H.264-GRP.mkv"
    assert misfiled.release_title(name) == "Volcano"
    assert misfiled.release_number(name) == "S01E02"
    assert misfiled.titles_agree("Blood in the Water", "Blood In The Water")


def test_a_file_both_clues_place_elsewhere_is_moved(app, monkeypatch):
    _show()
    # Filed as E2 "Bakery", downloaded as "Volcano" (E3), and it talks volcanoes.
    sonarr = FakeSonarr(
        [
            _episode(1, 11),
            _episode(2, 12),
            _episode(3),
            _episode(4, 14),
            _episode(5, 15),
        ],
        [_imported(102, 12, "Show.S01E02.Volcano.1080p.WEB")],
        {12: {"id": 12, "path": "/tv/Show/S01E02.mkv", "quality": {}}},
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: sonarr)
    talk = "lava volcano erupting molten geologists sampling climb " * 5
    monkeypatch.setattr(misfiled, "_transcript", _said({102: talk}))
    monkeypatch.setattr(autofix, "ON_BY_DEFAULT", True)
    assert misfiled.sweep() == 1
    imported = next(c for c in sonarr.calls if c[0] == "ManualImport")
    assert imported[1]["files"][0]["episodeIds"] == [103]
    assert ("search", [102]) in sonarr.calls  # the real E2 is looked for
    # The release itself said S01E02: blocklisted first, or the search brings
    # the same file straight back.
    assert sonarr.calls.index(("blocklist", 900)) < sonarr.calls.index(
        ("search", [102])
    )
    assert misfiled.rows() == []
    # Looked at again straight away: the move may free another episode's slot.
    assert "checked_at" not in setting(misfiled.KEY)


def test_when_what_is_said_fits_where_it_is_filed_nothing_is_listed(app, monkeypatch):
    _show()
    sonarr = FakeSonarr(
        [
            _episode(1, 11),
            _episode(2, 12),
            _episode(3),
            _episode(4, 14),
            _episode(5, 15),
        ],
        [_imported(102, 12, "Show.S01E02.Volcano.1080p.WEB")],
        {12: {"id": 12, "path": "/tv/Show/S01E02.mkv", "quality": {}}},
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: sonarr)
    talk = "croissants pastry chef bakes royal wedding banquet " * 5
    monkeypatch.setattr(misfiled, "_transcript", _said({102: talk}))
    monkeypatch.setattr(autofix, "ON_BY_DEFAULT", True)
    assert misfiled.sweep() == 0
    assert misfiled.rows() == [] and sonarr.calls == []


def test_without_a_transcript_a_person_decides(app, client, monkeypatch):
    media = _show()
    sonarr = FakeSonarr(
        [
            _episode(1, 11),
            _episode(2, 12),
            _episode(3),
            _episode(4, 14),
            _episode(5, 15),
        ],
        [_imported(102, 12, "Show.S01E02.Volcano.1080p.WEB")],
        {12: {"id": 12, "path": "/tv/Show/S01E02.mkv", "quality": {}}},
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: sonarr)
    monkeypatch.setattr(misfiled, "_transcript", _said({}))
    monkeypatch.setattr(autofix, "ON_BY_DEFAULT", True)
    assert misfiled.sweep() == 0
    (row,) = misfiled.rows()
    assert row["plan"] is None and row["media"].id == media.id
    page = client.get("/chapters/problems").text
    assert "S01E02 “Bakery” is really S01E03 “Volcano”" in page
    assert "been transcribed" in page
    client.post("/chapters/problems/misfiled/12/fix", data={"mode": "move"})
    assert any(c[0] == "ManualImport" for c in sonarr.calls)


def test_a_chain_moves_one_slot_at_a_time():
    # E2's file is E1; E1's file is the same episode as E1 (a duplicate).
    findings = {
        12: {"verdict": "other", "target_file_id": 11, "to_id": 101},
        11: {"verdict": "own", "target_file_id": 0, "to_id": 200},
        13: {"verdict": "other", "target_file_id": 12, "to_id": 102},
    }
    assert misfiled._plan(findings[13], findings) == "wait"  # E2's file moves first
    findings[12]["target_is_target"] = True
    findings[12]["better_than_target"] = False
    assert misfiled._plan(findings[12], findings) == "discard"


def test_checked_every_half_hour(app, monkeypatch):
    calls = []
    monkeypatch.setattr(misfiled, "_findings", lambda *_a: calls.append(1) or {})
    monkeypatch.setattr("curatarr.services.client", lambda _kind: None)
    misfiled.sweep()
    misfiled.sweep()
    assert len(calls) == 1
    set_setting(
        misfiled.KEY, {"checked_at": (utcnow() - timedelta(hours=1)).isoformat()}
    )
    misfiled.sweep()
    assert len(calls) == 2 and setting(misfiled.KEY)["findings"] == {}


def test_a_file_its_subtitles_show_is_another_episode_is_fixed(app, monkeypatch):
    from curatarr.models import ChapterState, MediaPart

    media = _show()
    states = {}
    for number in (2, 3):
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
            file_path=f"/tv/Show/S01E0{number}.mkv",
        )
        db.session.add(part)
        db.session.flush()
        states[number] = ChapterState(media_part_id=part.id, media_identity_id=media.id)
        db.session.add(states[number])
    # E2's file is really E3 (its speech is in E3's subtitles), and E3's own
    # file matches its own subtitles: E2's is a second copy of E3. Its
    # download was named for E2, so the name gave nothing away.
    states[2].subtitle_sync = {
        "files": {"S01E02.en.srt": {"outcome": "set_aside"}},
        "episode": {"fits": 3, "found": 140, "spoken": 150, "file": "S01E02.mkv"},
    }
    states[3].subtitle_sync = {"files": {"S01E03.en.srt": {"outcome": "in_sync"}}}
    db.session.commit()
    sonarr = FakeSonarr(
        [_episode(1, 11), _episode(2, 12), _episode(3, 13), _episode(4, 14)],
        [],
        {
            12: {"id": 12, "path": "/tv/Show/S01E02.mkv", "quality": {}},
            13: {"id": 13, "path": "/tv/Show/S01E03.mkv", "quality": {}},
        },
    )
    monkeypatch.setattr("curatarr.services.client", lambda _kind: sonarr)
    monkeypatch.setattr(misfiled, "_transcript", _said({}))
    monkeypatch.setattr(autofix, "ON_BY_DEFAULT", True)
    from curatarr import filecheck_jobs

    monkeypatch.setattr(filecheck_jobs, "_dry_run", lambda media: False)
    assert misfiled.sweep() == 1
    assert ("delete", 12) in sonarr.calls  # the second copy
    assert ("blocklist", 900) in sonarr.calls  # the download that held it
    assert sonarr.calls.index(("blocklist", 900)) < sonarr.calls.index(
        ("search", [102])
    )
    lines = misfiled.evidence(
        next(iter((setting(misfiled.KEY) or {})["findings"].values()))
    )
    assert "its own subtitles don't match" in lines[0]
    assert "140 of 150 lines heard" in lines[1] and "second copy" in lines[2]


def test_subtitle_evidence_about_a_replaced_file_is_ignored(app, monkeypatch):
    from curatarr.models import ChapterState, MediaPart

    media = _show()
    part = MediaPart(
        media_identity_id=media.id,
        kind="episode",
        season_number=1,
        episode_number=2,
        has_file=True,
        file_path="/tv/Show/S01E02.new.mkv",
    )
    db.session.add(part)
    db.session.flush()
    db.session.add(
        ChapterState(
            media_part_id=part.id,
            media_identity_id=media.id,
            subtitle_sync={
                "episode": {
                    "fits": 3,
                    "found": 140,
                    "spoken": 150,
                    "file": "S01E02.mkv",
                }
            },
        )
    )
    db.session.commit()
    sonarr = FakeSonarr([_episode(2, 12), _episode(3, 13)], [], {})
    monkeypatch.setattr("curatarr.services.client", lambda _kind: sonarr)
    assert misfiled._findings(sonarr, utcnow()) == {}
