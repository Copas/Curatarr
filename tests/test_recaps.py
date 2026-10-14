from curatarr import recaps
from curatarr.chapters import DEFAULTS as CHAPTER_DEFAULTS
from curatarr.chapters import RECAP_CHAPTER_NAME, Segment, semantic_plan


def lines(*rows):
    return [Segment(start, end, text) for start, end, text in rows]


EVENTS = {
    18: ("the blue tribe lost the raft race", "Marco hid the machete", "camp", "rice"),
    17: (
        "Lena found an idol in the swamp",
        "the red tribe stopped talking",
        "beach",
        "fish",
    ),
    16: (
        "a storm flattened every shelter",
        "Priya swore revenge on Tom",
        "cliff",
        "rain",
    ),
    15: ("the merge split old alliances", "Dev lied about the vote", "fire", "sleep"),
    12: (
        "two castaways fought over food",
        "the jury watched in silence",
        "hut",
        "crab",
    ),
}


def narrated(left: int, closer_at=80.0, extra=""):
    """A reality-show recap: narration and clips that change weekly, the same
    closing line every week, then a new scene."""
    event, clip, place, food = EVENTS[left]
    return lines(
        (0.5, 3.0, "Previously on Island Life."),
        (3.0, 20.0, f"This week {event}."),
        (20.0, 40.0, f"Everyone says {clip}."),
        (40.0, 60.0, f"{place.title()}person, the tribe has spoken."),
        (60.0, closer_at - 5, f"I had no idea, {food} everywhere."),
        (
            closer_at - 5,
            closer_at,
            f"{left} are left, who will be voted out tonight?{extra}",
        ),
        (closer_at + 10, closer_at + 15, f"Back at the {place} it was quiet."),
        (closer_at + 15, closer_at + 30, f"We need more {food} or we won't last."),
    )


SERIES = [narrated(n, 70 + n) for n in (18, 17, 16, 15)]


def test_closing_line_is_learned_and_ends_the_recap():
    closers = recaps.closing_phrases([recaps.opening_lines(e) for e in SERIES])
    assert ("who", "will", "be", "voted") in closers
    assert ("the", "tribe", "has", "spoken") not in closers  # too short: mid-recap
    assert ("i", "had", "no", "idea") not in closers  # too short to be a closing line
    episode = narrated(12, 95)
    found = recaps.find_recap(
        episode, series_openings=[recaps.opening_lines(e) for e in SERIES]
    )
    assert found == recaps.Recap(0.0, 95.0, "closing line")


def test_theme_lyrics_after_the_recap_do_not_extend_it():
    lyrics = " Here we go again, sailing on the open sea tonight."
    series = []
    for n in (18, 17, 16, 15):
        episode = narrated(n, 70 + n)
        episode.insert(6, Segment(72 + n, 80 + n, lyrics))
        series.append(episode)
    found = recaps.find_recap(
        series[0], series_openings=[recaps.opening_lines(e) for e in series[1:]]
    )
    assert found.end == 88.0  # the closing line, not the song after it


def test_no_opener_means_no_recap():
    cold_open = lines(
        (0, 30, "Okay everybody, the meeting starts now."),
        (30, 60, "Who took my stapler?"),
    )
    assert (
        recaps.find_recap(
            cold_open, series_openings=[recaps.opening_lines(e) for e in SERIES]
        )
        is None
    )


def test_unclear_end_marks_nothing():
    """An opener with no closing line and no earlier episodes to match: leave it."""
    assert recaps.find_recap(narrated(12, 95)) is None


def test_closer_merged_with_the_next_line_is_cut_short():
    episode = narrated(12, 95, extra=" i dreamed i got shot no way")
    found = recaps.find_recap(
        episode, series_openings=[recaps.opening_lines(e) for e in SERIES]
    )
    assert 90 < found.end < 95


def test_scene_cut_near_the_end_is_used():
    found = recaps.find_recap(
        narrated(12, 95),
        series_openings=[recaps.opening_lines(e) for e in SERIES],
        scenes=[50.0, 97.5, 140.0],
    )
    assert found.end == 97.5


def montage(quotes):
    return lines(
        (0.0, 2.0, "Previously on The Agency."),
        *[(3.0 + 10 * i, 10.0 + 10 * i, q) for i, q in enumerate(quotes)],
        (60.0, 66.0, "Narrator says everything changed after that night."),
        (80.0, 85.0, "Good morning, the coffee machine is broken again."),
    )


def test_montage_ends_after_the_last_quoted_clip_and_the_model_refines_it():
    earlier_episode = lines(
        (100, 105, "You were never supposed to see that file."),
        (200, 205, "Pack your bags, we leave for Berlin tonight."),
        (300, 305, "I trusted you and you lied to me again."),
        (
            2400,
            2410,
            "Next time: the coffee machine is broken again forever.",
        ),  # preview
    )
    earlier = recaps.earlier_grams([earlier_episode])
    episode = montage(
        [
            "You were never supposed to see that file.",
            "Pack your bags, we leave for Berlin tonight.",
            "I trusted you and you lied to me again.",
        ]
    )
    anchor_only = recaps.find_recap(episode, earlier=earlier)
    # 9 words from 0:23: the words stop at 0:28.5 even though the line runs on.
    assert anchor_only.source == "earlier episodes" and anchor_only.end == 28.5
    asked = []

    def choose(candidates):
        asked.append([c.text for c in candidates])
        return 1  # the narrator's line

    refined = recaps.find_recap(episode, earlier=earlier, choose_last_line=choose)
    assert refined == recaps.Recap(0.0, 64.5, "model")  # 7 words from 1:00
    assert asked[0][0].startswith("I trusted you")
    # The preview at the end of the earlier episode quotes the new one; ignored.
    assert not any("coffee" in line for line in asked[0][:2])


def test_recap_becomes_the_first_chapter_and_planning_starts_after_it():
    config = {
        **CHAPTER_DEFAULTS,
        "target_spacing_minutes": 10,
        "minimum_spacing_minutes": 5,
        "maximum_spacing_minutes": 15,
    }
    segments = [Segment(t, t + 5, f"line {t}") for t in range(0, 2400, 30)]
    plan = semantic_plan(
        2400.0,
        [float(t) for t in range(100, 2400, 120)],
        segments,
        config,
        lambda window, candidates, excerpt: 0,
        lambda excerpts: [f"Part {i}" for i, _ in enumerate(excerpts, 1)],
        recap=recaps.Recap(0.0, 95.0, "closing line"),
    )
    assert plan.chapters[0].name == RECAP_CHAPTER_NAME
    assert plan.chapters[1].start == 95.0 and plan.chapters[1].name == "Part 1"
    assert plan.chapters[2].start >= 95.0 + 5 * 60
    assert any("Recap until" in note for note in plan.notes)


def test_a_stray_match_after_the_recap_does_not_stretch_it():
    """Battlestar Galactica S01E03: a common phrase two minutes in had matched an
    earlier episode, and the recap was marked to 3:06 instead of about 1:05."""
    earlier = recaps.earlier_grams(
        [
            lines(
                (500, 506, "Tell the captain I expect daily reports on his prisoners."),
                (900, 903, "So what do we have here then everybody?"),
            )
        ]
    )
    episode = lines(
        (2.7, 5.0, "Previously on Battlestar Galactica."),
        (41.2, 44.0, "Tell the captain I expect daily reports on his prisoners."),
        (61.7, 64.0, "We'll run out of water in two days."),
        (111.2, 113.0, "So what do we have here then everybody?"),  # new material
        (113.2, 118.0, "We scouted the ice moon and found an ocean."),
    )
    found = recaps.find_recap(episode, earlier=earlier)
    assert found is not None and found.end < 60


def test_a_line_stretched_over_music_ends_when_its_words_do():
    stretched = Segment(67.2, 186.1, "I'm fine. I'm just...")
    assert recaps.spoken_end(stretched) < 72
