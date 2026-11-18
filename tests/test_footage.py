from types import SimpleNamespace

import numpy as np

from curatarr import credits, footage
from curatarr.enrichment import INTRO_RULE, _place_intro, intro_verdict


def _print(seconds, seed):
    """A fingerprint of ``seconds`` of varied pictures, and no sound."""
    rng = np.random.default_rng(seed)
    thumbs = rng.integers(0, 255, (seconds, footage.H, footage.W), dtype=np.uint8)
    hashes, flat = footage.picture_hashes(thumbs)
    return footage.Print(hashes, flat, None), thumbs


def test_the_intro_is_the_footage_the_season_shares():
    titles = np.random.default_rng(99).integers(
        0, 255, (40, footage.H, footage.W), dtype=np.uint8
    )
    episodes = []
    for n, at in enumerate((60, 75, 50, 90, 65)):
        _unused, thumbs = _print(1200, n)
        thumbs[at : at + 40] = titles  # the same titles, a different place
        thumbs[5:9] = titles[:4]  # a 4-second logo: too short to be the intro
        hashes, flat = footage.picture_hashes(thumbs)
        episodes.append(footage.Print(hashes, flat, None))
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0) == (60, 100)
    assert footage.intro_block(episodes[0], episodes[1:3], 1200.0) is None  # too few


def test_the_intro_is_the_shared_stretch_by_intro_skippers_not_a_longer_one():
    rng = np.random.default_rng(7)
    titles = rng.integers(0, 255, (30, footage.H, footage.W), dtype=np.uint8)
    studio = rng.integers(0, 255, (60, footage.H, footage.W), dtype=np.uint8)
    episodes = []
    for n in range(5):
        _unused, thumbs = _print(1200, 20 + n)
        thumbs[0:30] = titles  # the title sequence
        thumbs[100:160] = studio  # a longer shared studio opening, later
        hashes, flat = footage.picture_hashes(thumbs)
        episodes.append(footage.Print(hashes, flat, None))
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0) == (100, 160)
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0, (0, 22)) == (0, 30)
    assert footage.intro_block(episodes[0], episodes[1:], 1200.0, (300, 320)) is None


def test_blank_pictures_are_no_evidence():
    thumbs = np.zeros((10, footage.H, footage.W), np.uint8)
    _hashes, flat = footage.picture_hashes(thumbs)
    assert flat.all()


def test_intro_skippers_intro_only_ever_widens():
    assert footage.widened((40.0, 68.0), (37.0, 72.0)) == (37.0, 72.0)
    # an edge further out than WIDEN, or further in, stays Intro Skipper's
    assert footage.widened((213.0, 276.0), (211.0, 287.0)) == (211.0, 276.0)
    assert footage.widened((0.0, 24.0), (3.0, 28.0)) == (0.0, 28.0)
    assert footage.widened((0.0, 30.0), (2.0, 30.0)) is None
    # under a second further out isn't worth a change
    assert footage.widened((78.7, 108.1), (78.0, 108.1)) is None
    assert footage.widened((78.7, 108.1), (77.7, 108.5)) == (77.7, 108.1)
    assert footage.widened((0.0, 30.0), (300.0, 330.0)) is None  # elsewhere
    assert footage.snap(100.0, [(97.4, "cut"), (101.0, "black")]) == 101.0
    assert footage.snap(100.0, [(90.0, "cut")]) == 100.0


class FakePlugin:
    def __init__(self):
        self.added, self.hidden, self.restored = [], [], []

    def add(self, item_id, kind, start, end):
        self.added.append((kind, start, end))
        return f"ours-{len(self.added)}"

    def hide(self, item_id, segment_id):
        self.hidden.append(segment_id)

    def restore(self, item_id, segment_id):
        self.restored.append(segment_id)


def _ep(block, rule=None, ours=None, titles="pictures", run_on=0):
    return SimpleNamespace(
        info=SimpleNamespace(duration=2600.0, path="file"),
        state=SimpleNamespace(id="state", intro_rule=rule, intro_found=None),
        intro_block=lambda near=None: block,
        season_prints=lambda: ("mine", ["others"]),
        own_intro=lambda: ours,
        titles_evidence=lambda block: titles,
        run_on=lambda end: run_on,
    )


def test_intro_skippers_intro_is_replaced_by_a_wider_one(monkeypatch):
    monkeypatch.setattr(credits, "frames", lambda *_a: np.zeros((1, 1, 1)))
    monkeypatch.setattr(credits, "find_breaks", lambda _p, start: [])
    theirs = {"Id": "theirs", "Start": 40.0, "End": 68.0, "Source": "Chromaprint"}
    plugin, hidden = FakePlugin(), []
    assert _place_intro(_ep((37, 72)), plugin, "item", [theirs], hidden)
    assert plugin.added == [("Introduction", 37, 72)] and plugin.hidden == ["theirs"]
    assert hidden[0]["replaced_by"] == "ours-1" and hidden[0]["type"] == "Introduction"
    # checked again by the same rules: nothing to do
    assert (
        _place_intro(_ep((37, 72), INTRO_RULE), FakePlugin(), "item", [], hidden)
        is None
    )
    # by older rules: put back, then widened again
    again = FakePlugin()
    assert _place_intro(_ep((37, 72), 0), again, "item", [], hidden)
    assert again.restored == ["theirs"] and again.added == [("Introduction", 37, 72)]
    assert len(hidden) == 1
    # nothing shared, no intro, or two intros: left alone
    for block, intros in (
        (None, [theirs]),
        ((37, 72), []),
        ((37, 72), [theirs, {**theirs, "Id": "other"}]),
    ):
        plugin = FakePlugin()
        assert _place_intro(_ep(block), plugin, "item", intros, []) is None
        assert plugin.added == []


PER_SECOND = 8  # Chromaprint values a second, near enough


def _sounds(seconds, seed):
    return np.random.default_rng(seed).integers(
        0, 2**32, seconds * PER_SECOND, dtype=np.uint32
    )


def _noisy(values, seed, bits=2):
    """``values`` with ``bits`` random bits of each flipped (encoded again)."""
    rng = np.random.default_rng(seed)
    out = values.copy()
    for _ in range(bits):
        out ^= np.left_shift(
            np.uint32(1), rng.integers(0, 32, len(out)).astype(np.uint32)
        )
    return out


def _heard(sound, seconds):
    """A fingerprint with blank pictures (no evidence) and this sound."""
    thumbs = np.zeros((seconds, footage.H, footage.W), np.uint8)
    hashes, flat = footage.picture_hashes(thumbs)
    return footage.Print(hashes, flat, sound)


def test_sound_alike_but_not_identical_is_shared():
    titles = _sounds(40, 1)
    here, there = _sounds(600, 2), _sounds(600, 3)
    here[60 * PER_SECOND : 100 * PER_SECOND] = titles
    there[200 * PER_SECOND : 240 * PER_SECOND] = _noisy(titles, 4)
    shared = footage._sound_seconds(_heard(here, 600), _heard(there, 600), range(600))
    assert set(range(61, 99)) <= shared
    assert not shared - set(range(58, 101))  # unrelated sound isn't


def test_curatarrs_own_intro_is_the_sound_most_of_the_season_shares():
    titles, cue = _sounds(45, 10), _sounds(25, 11)
    episodes = []
    for n, at in enumerate((60, 75, 50, 90, 65, 80)):
        sound = _sounds(1200, 20 + n)
        sound[at * PER_SECOND : (at + 45) * PER_SECOND] = _noisy(titles, 30 + n)
        sound[5 * PER_SECOND : 13 * PER_SECOND] = titles[: 8 * PER_SECOND]  # a sting
        if n < 3:  # a music cue in some episodes, longer than the titles' sting
            sound[600 * PER_SECOND : 625 * PER_SECOND] = cue
        episodes.append(_heard(sound, 1200))
    found = footage.own_intro(episodes[0], episodes[1:], 1200.0)
    assert found and abs(found[0] - 60) <= 2 and abs(found[1] - 105) <= 2
    assert footage.own_intro(episodes[0], episodes[1:3], 1200.0) is None  # too few
    # no titles: the cue (2 of 5 others) and the sting (too short) aren't intros
    plain = [_heard(_sounds(1200, 50 + n), 1200) for n in range(4)]
    cued = episodes[0].sound.copy()
    cued[60 * PER_SECOND : 105 * PER_SECOND] = _sounds(45, 99)
    assert footage.own_intro(_heard(cued, 1200), episodes[1:3] + plain, 1200.0) is None


def test_how_curatarrs_intro_compares_with_intro_skippers():
    assert intro_verdict({"ours": [37, 72], "theirs": [[34.4, 72.4]]}) == "agree"
    assert intro_verdict({"ours": [50, 132], "theirs": [[44.0, 123.1]]}) == "edges"
    assert intro_verdict({"ours": [436, 557], "theirs": []}) == "missed"
    assert intro_verdict({"ours": None, "theirs": [[350.1, 386.6]]}) == "extra"
    assert intro_verdict({"ours": [1097, 1118], "theirs": [[40, 70]]}) == "elsewhere"
    assert intro_verdict({"ours": None, "theirs": []}) is None
    assert intro_verdict({"unknown": "too few", "theirs": [[40, 70]]}) is None


def test_curatarrs_own_intro_is_added_where_intro_skipper_has_none(monkeypatch):
    monkeypatch.setattr(credits, "frames", lambda *_a: np.zeros((1, 1, 1)))
    monkeypatch.setattr(credits, "find_breaks", lambda _p, start: [])
    plugin, hidden = FakePlugin(), []
    ep = _ep(None, ours=(436, 557))
    assert _place_intro(ep, plugin, "item", [], hidden) == "intro added"
    assert ep.state.intro_found == {
        "ours": [436, 557],
        "theirs": [],
        "verdict": "missed",
    }
    assert plugin.added == [("Introduction", 436, 557)] and plugin.hidden == []
    assert hidden[0]["id"] == "ours-1" and hidden[0]["added"]
    assert hidden[0]["type"] == "Introduction" and "found none" in hidden[0]["reason"]
    # checked again by newer rules: taken away, then added again
    again = FakePlugin()
    assert _place_intro(_ep(None, 0, (436, 557)), again, "item", [], hidden)
    assert again.hidden == ["ours-1"] and again.added == [("Introduction", 436, 557)]
    assert len(hidden) == 1
    # none found by either: nothing added
    plugin = FakePlugin()
    assert _place_intro(_ep(None), plugin, "item", [], []) is None
    assert plugin.added == []
    # shared sound that doesn't look like titles (music under the story): not
    # added, and the comparison says so
    plugin = FakePlugin()
    ep = _ep(None, ours=(1229, 1250), titles=None)
    assert _place_intro(ep, plugin, "item", [], []) is None
    assert plugin.added == [] and ep.state.intro_found["not_titles"]
    # one added under older rules that isn't titles: taken away, and said so
    again, earlier = FakePlugin(), [{**hidden[0]}]
    ep = _ep(None, 0, (436, 557), titles=None)
    assert _place_intro(ep, again, "item", [], earlier) == "intro removed"
    assert again.hidden == ["ours-1"] and again.added == [] and earlier == []


def test_a_title_sequence_shows_itself_a_music_cue_doesnt():
    rng = np.random.default_rng(5)
    titles = rng.integers(0, 255, (30, footage.H, footage.W), dtype=np.uint8)
    music = _sounds(30, 6)
    episodes, scenes = [], []
    for n, at in enumerate((60, 80, 70, 90, 75)):
        _unused, thumbs = _print(600, 40 + n)
        thumbs[at : at + 30] = titles
        sound = _sounds(600, 60 + n)
        sound[at * PER_SECOND : (at + 30) * PER_SECOND] = _noisy(music, 70 + n)
        hashes, flat = footage.picture_hashes(thumbs)
        episodes.append(footage.Print(hashes, flat, sound))
        # the same music, but under a different scene each time
        _unused, plain = _print(600, 80 + n)
        hashes, flat = footage.picture_hashes(plain)
        scenes.append(footage.Print(hashes, flat, sound))
    here, others = episodes[0], episodes[1:]
    assert footage.picture_share(here, others, (60, 90)) > 0.8
    assert footage.picture_share(scenes[0], scenes[1:], (60, 90)) == 0.0
    # in the others, the sound is where Intro Skipper found their intros
    theirs = [[(80, 110)], [(70, 100)], [(90, 120)], [(75, 105)]]
    assert footage.sound_in_intros(here, list(zip(others, theirs)), (60, 90)) == (4, 4)
    elsewhere = [[(400, 430)]] * 4
    assert footage.sound_in_intros(here, list(zip(others, elsewhere)), (60, 90)) == (
        0,
        4,
    )
    assert footage.sound_in_intros(here, [(o, []) for o in others], (60, 90)) == (0, 0)
    # any one is enough; lettering is only read when the rest isn't
    read = []

    def lettered():
        read.append(1)
        return 0

    assert footage.titles_evidence(0.5, (0, 0), lettered) == "pictures"
    assert footage.titles_evidence(0.0, (3, 4), lettered) == "intros"
    assert read == []
    assert footage.titles_evidence(0.0, (1, 4), 5) == "lettering"
    assert footage.titles_evidence(0.02, (2, 4), lettered) is None and read


def test_intro_skippers_intro_ends_where_curatarrs_does(monkeypatch):
    monkeypatch.setattr(credits, "frames", lambda *_a: np.zeros((1, 1, 1)))
    monkeypatch.setattr(credits, "find_breaks", lambda _p, start: [])
    theirs = {"Id": "theirs", "Start": 127.0, "End": 211.3, "Source": "Chromaprint"}
    plugin, hidden = FakePlugin(), []
    # the footage alone would widen it less: Curatarr's own intro goes first
    ep = _ep((125, 214), ours=(124, 220))
    assert _place_intro(ep, plugin, "item", [theirs], hidden) == "intro widened"
    assert plugin.added == [("Introduction", 124, 220)] and plugin.hidden == ["theirs"]
    assert "sound the season shares" in hidden[0]["reason"]
    # an end more than OWN_WIDEN later, or earlier, stays Intro Skipper's
    plugin = FakePlugin()
    late = theirs["End"] + footage.OWN_WIDEN + 5
    ep = _ep(None, ours=(127, late))
    assert _place_intro(ep, plugin, "item", [theirs], []) is None
    assert plugin.added == []
    plugin = FakePlugin()
    assert (
        _place_intro(_ep(None, ours=(127, 190)), plugin, "item", [theirs], []) is None
    )
    # Curatarr's own somewhere else: the footage widens it as before
    plugin = FakePlugin()
    ep = _ep((125, 214), ours=(600, 650))
    assert _place_intro(ep, plugin, "item", [theirs], [])
    assert plugin.added == [("Introduction", 125, 214)]


def test_the_intro_comparison_lists_where_the_two_disagree(app, client):
    from curatarr import db
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart

    library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
    db.session.add(show)
    db.session.flush()
    found = (
        {"ours": [37, 72], "theirs": [[34.4, 72.4]], "verdict": "agree"},
        {"ours": [436, 557], "theirs": [], "verdict": "missed"},
        {"ours": None, "theirs": [[350.1, 386.6]], "verdict": "extra"},
        {"ours": [1229, 1250], "theirs": [], "verdict": "missed", "not_titles": True},
    )
    for number, intro in enumerate(found, 1):
        part = MediaPart(
            media_identity_id=show.id,
            kind="episode",
            season_number=1,
            episode_number=number,
            has_file=True,
            file_path=f"/tv/Show/S01E0{number}.mkv",
        )
        db.session.add(part)
        db.session.flush()
        state = ChapterState(media_part_id=part.id, media_identity_id=show.id)
        state.intro_found = intro
        db.session.add(state)
    db.session.commit()
    page = client.get("/chapters/markers?kind=intros").get_data(as_text=True)
    assert "Every disagreement · 3" in page and "Agree · 1" in page
    assert page.count("Not added: shared music") == 1  # a cue under the story
    assert "7:16" in page and "9:17" in page  # Curatarr's, where Intro Skipper has none
    assert "0:37" not in page  # agreeing isn't listed…
    page = client.get("/chapters/markers?kind=intros&verdict=agree").get_data(
        as_text=True
    )
    assert "0:37" in page  # …unless asked for
    assert client.get("/chapters/markers?kind=intros&verdict=nope").status_code == 404


def test_an_intro_runs_on_while_the_titles_pictures_are_the_seasons(monkeypatch):
    """Taskmaster: the shared music stops at 0:26 while every episode still
    shows the same last shots to 0:30; the intro ends there, before the
    episode's own title card."""
    monkeypatch.setattr(credits, "frames", lambda *_a: np.zeros((1, 1, 1)))
    monkeypatch.setattr(credits, "find_breaks", lambda _p, start: [])
    theirs = {"Id": "theirs", "Start": 0.0, "End": 24.0, "Source": "Chromaprint"}
    # Curatarr's own agrees (0-25): nothing to widen, but the titles run on
    plugin, hidden = FakePlugin(), []
    ep = _ep(None, ours=(0, 25), run_on=4)
    assert _place_intro(ep, plugin, "item", [theirs], hidden) == "intro widened"
    assert plugin.added == [("Introduction", 0.0, 29)]
    assert "stop showing the same pictures" in hidden[0]["reason"]
    # a second or less isn't worth a change
    plugin = FakePlugin()
    assert (
        _place_intro(_ep(None, ours=(0, 24), run_on=0), plugin, "item", [theirs], [])
        is None
    )
    # an intro Curatarr adds runs on too
    plugin = FakePlugin()
    assert _place_intro(_ep(None, ours=(436, 557), run_on=6), plugin, "item", [], [])
    assert plugin.added == [("Introduction", 436, 563)]


def test_run_on_counts_only_new_keyframes_the_season_shares():
    rng = np.random.default_rng(3)
    shots = rng.integers(0, 255, (6, footage.H, footage.W), dtype=np.uint8)
    scene = rng.integers(0, 255, (40, footage.H, footage.W), dtype=np.uint8)

    def episode(seed, own_scene):
        _unused, thumbs = _print(120, seed)
        thumbs[30:36] = shots  # the titles' last shots, the same everywhere
        if own_scene:
            # the last shot's keyframe repeats over the next scene (no new
            # keyframe until 46), then the scene
            thumbs[36:46] = shots[-1]
            thumbs[46:86] = scene
        hashes, flat = footage.picture_hashes(thumbs)
        return footage.Print(hashes, flat, None, footage.dark_and_plain(thumbs))

    here = episode(1, True)
    others = [episode(10 + n, False) for n in range(5)]
    # shared from 30 to 35; the repeated keyframe after it doesn't count
    assert footage.run_on(here, others, 30) == 6
    # nothing shared past the end: no run-on
    assert footage.run_on(here, others, 60) == 0
    # a dark, plain picture stops it (it would match the like anywhere)
    shots[3] = 8
    here = episode(1, True)
    others = [episode(10 + n, False) for n in range(5)]
    assert footage.run_on(here, others, 30) == 3
