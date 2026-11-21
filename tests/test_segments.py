"""Curatarr keeps every item's skip segments itself (segments.py), so its
Jellyfin plugin can give them to Jellyfin instead of Intro Skipper."""

from test_acquisition_reconcile import _show

from curatarr import db, segments
from curatarr.models import MediaSegment
from curatarr.services import set_setting, setting


class Plugin:
    """Intro Skipper: one episode's segments, which its own analysis can change."""

    def __init__(self, _jellyfin=None):
        self.live = {
            "ep1": [{"Id": "is-1", "Type": "Introduction", "Start": 0.0, "End": 30.0}]
        }
        self.made = 0

    def segments(self, item_id):
        return [dict(s) for s in self.live.get(item_id, [])]

    def add(self, item_id, kind, start, end):
        self.made += 1
        sid = f"ours-{self.made}"
        self.live.setdefault(item_id, []).append(
            {"Id": sid, "Type": kind, "Start": start, "End": end}
        )
        return sid

    def hide(self, item_id, sid):
        self.live[item_id] = [s for s in self.live.get(item_id, []) if s["Id"] != sid]

    def restore(self, item_id, sid):
        pass


def _store(monkeypatch, plugin):
    monkeypatch.setattr("curatarr.integrations.IntroSkipper", lambda _j: plugin)
    return segments.SegmentStore(object())


def test_segments_are_recorded_as_they_go_to_intro_skipper(app, monkeypatch):
    plugin = Plugin()
    store = _store(monkeypatch, plugin)
    assert [s["Id"] for s in store.segments("ep1")] == ["is-1"]  # taken in
    recap = store.add("ep1", "Recap", 0.0, 62.4)
    store.hide("ep1", "is-1")
    db.session.commit()
    rows = {r.id: r for r in db.session.query(MediaSegment)}
    assert rows["is-1"].hidden and not rows[recap].hidden
    assert rows["is-1"].version > rows[recap].version  # hidden after the add
    # Intro Skipper's own analysis moved its intro: reading takes that in
    plugin.live["ep1"] = [
        {"Id": "is-2", "Type": "Introduction", "Start": 5.0, "End": 35.0}
    ]
    store.segments("ep1")
    db.session.commit()
    visible = {r.id for r in db.session.query(MediaSegment).filter_by(hidden=False)}
    assert visible == {"is-2"}


def test_the_plugin_fetches_what_changed_in_jellyfins_terms(app, client, monkeypatch):
    plugin = Plugin()
    store = _store(monkeypatch, plugin)
    store.segments("ep1")
    store.add("ep1", "Credits", 2500.0, 2600.0)
    db.session.commit()
    everything = segments.changed_since(0)
    assert everything["items"] == [
        {
            "item": "ep1",
            "segments": [
                {"type": "Intro", "start": 0.0, "end": 30.0},
                {"type": "Outro", "start": 2500.0, "end": 2600.0},
            ],
        }
    ]
    assert segments.changed_since(everything["version"])["items"] == []
    store.hide("ep1", "is-1")
    db.session.commit()
    after = segments.changed_since(everything["version"])["items"]
    assert after == [
        {"item": "ep1", "segments": [{"type": "Outro", "start": 2500.0, "end": 2600.0}]}
    ]
    # over the API, with the plugin's token only
    assert client.get("/api/v1/jellyfin/segments").status_code == 403
    set_setting("webhook_token", "test-token")
    db.session.commit()
    response = client.get(
        "/api/v1/jellyfin/segments?after=0", headers={"X-Curatarr-Token": "test-token"}
    )
    assert response.status_code == 200 and response.json["items"][0]["item"] == "ep1"


def test_every_episode_is_read_once_to_start_the_store(app, monkeypatch):
    _media, parts = _show("Tasks", [(1, 1, True), (1, 2, True)])
    for n, part in enumerate(parts.values(), 1):
        part.jellyfin_id = f"ep{n}"
    db.session.commit()
    plugin = Plugin()
    plugin.live["ep2"] = [{"Id": "is-9", "Type": "Credits", "Start": 1.0, "End": 2.0}]
    monkeypatch.setattr("curatarr.integrations.IntroSkipper", lambda _j: plugin)
    monkeypatch.setattr("curatarr.services.client", lambda _kind: object())
    assert segments.import_sweep() == 2
    assert setting(segments.IMPORT_KEY)["finished_at"]
    assert {r.id for r in db.session.query(MediaSegment)} == {"is-1", "is-9"}
    assert segments.import_sweep() == 0  # once


def test_without_intro_skipper_the_store_stands_alone(app):
    store = segments.SegmentStore(forward=False)
    sid = store.add("ep1", "Introduction", 0.0, 30.0)
    db.session.commit()
    assert store.segments("ep1") == [
        {"Id": sid, "Type": "Introduction", "Start": 0.0, "End": 30.0}
    ]
    store.hide("ep1", sid)
    assert store.segments("ep1") == []
    store.restore("ep1", sid)
    assert [s["Id"] for s in store.segments("ep1")] == [sid]


def test_an_episode_intro_skipper_doesnt_know_doesnt_stop_the_import(app, monkeypatch):
    """Intro Skipper answers "not found" for an episode it never analysed:
    the import stopped there for good, taking it for "not installed"."""
    _media, parts = _show("Tasks", [(1, 1, True), (1, 2, True)])
    for n, part in enumerate(parts.values(), 1):
        part.jellyfin_id = f"ep{n}"
    db.session.commit()
    plugin = Plugin()
    unknown = {"ep1"}
    real = plugin.segments
    plugin.segments = lambda item_id: None if item_id in unknown else real(item_id)
    plugin.live["ep2"] = [{"Id": "is-9", "Type": "Credits", "Start": 1.0, "End": 2.0}]
    monkeypatch.setattr("curatarr.integrations.IntroSkipper", lambda _j: plugin)
    monkeypatch.setattr("curatarr.services.client", lambda _kind: object())
    assert segments.import_sweep() == 1
    assert setting(segments.IMPORT_KEY)["finished_at"]
    assert {r.id for r in db.session.query(MediaSegment)} == {"is-9"}


def test_segments_intro_skipper_hides_are_kept_hidden(app, monkeypatch):
    """Intro Skipper lists the segments it hides, flagged Suppressed (its own
    intro that Curatarr replaced): taken as shown, they'd have been served
    beside their replacements (6 items with two intros, 59 with two credits)."""
    plugin = Plugin()
    plugin.live["ep1"] = [
        {"Id": "is-1", "Type": "Introduction", "Start": 25.4, "End": 116.0, "Suppressed": True},
        {"Id": "ours-1", "Type": "Introduction", "Start": 22.8, "End": 120.6},
    ]  # fmt: skip
    store = _store(monkeypatch, plugin)
    store.segments("ep1")
    db.session.commit()
    assert segments.changed_since(0)["items"] == [
        {"item": "ep1", "segments": [{"type": "Intro", "start": 22.8, "end": 120.6}]}
    ]


def test_a_new_import_rule_reads_everything_again(app, monkeypatch):
    set_setting(segments.IMPORT_KEY, {"after": "zzz", "finished_at": "2026-11-21"})
    db.session.commit()
    monkeypatch.setattr("curatarr.integrations.IntroSkipper", lambda _j: Plugin())
    monkeypatch.setattr("curatarr.services.client", lambda _kind: object())
    segments.import_sweep()
    assert setting(segments.IMPORT_KEY)["rule"] == segments.IMPORT_RULE
