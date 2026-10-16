from curatarr import markers, recaps
from curatarr.chapters import Segment


def lines(*rows):
    return [Segment(start, end, text) for start, end, text in rows]


DURATION = 2600.0


def survivor_ending(final_words=True):
    rows = [
        (2500.0, 2505.0, "Grab a torch and head back to camp. Good night."),
        (2531.0, 2534.0, "Stay tuned for scenes from our next episode."),
        (2535.0, 2537.0, "Next time on Island Life,"),
        (2537.0, 2541.0, "Dan wants a bus ticket back to New York."),
        (2541.0, 2547.0, "Tribal immunity is not what you're playing for today."),
    ]
    if final_words:
        rows += [
            (
                2560.0,
                2566.0,
                "My plan for this tribe was to stay as strong as we could.",
            ),
            (
                2566.0,
                2575.0,
                "So I'm not real surprised, anything can happen out here.",
            ),
        ]
    return lines(*rows)


def test_preview_needs_a_reliable_phrase():
    dialogue = lines((2550.0, 2555.0, "Maybe next time we'll catch a fish."))
    assert markers.find_preview(dialogue, DURATION) is None
    assert markers.preview_opener(dialogue, DURATION) is None


def test_preview_end_from_clips_quoted_in_the_next_episode():
    next_episode = [
        lines(
            (300.0, 305.0, "Dan wants a bus ticket back to New York."),
            (900.0, 905.0, "Tribal immunity is not what you're playing for today."),
            (2000.0, 2005.0, "Filler line about the weather on the beach today."),
        )
    ]
    found = markers.find_preview(
        survivor_ending(), DURATION, later=recaps.earlier_grams(next_episode)
    )
    assert found == recaps.Recap(2530.5, 2547.0, "next episode")  # not the final words


def test_model_chosen_preview_end_must_be_confirmed():
    pick_tribal = lambda candidates: 3
    yes = markers.find_preview(
        survivor_ending(),
        DURATION,
        choose_last_line=pick_tribal,
        confirm=lambda _: True,
    )
    assert yes.end == 2547.0 and yes.source == "model"
    assert (
        markers.find_preview(
            survivor_ending(),
            DURATION,
            choose_last_line=pick_tribal,
            confirm=lambda _: False,
        )
        is None
    )


def test_preview_that_runs_into_the_end_of_the_file():
    ending = survivor_ending(final_words=False)
    found = markers.find_preview(ending, 2560.0)
    assert found.end == 2560.0 and found.source == "runs to the end"


def credits(start, end, source="Chromaprint", segment_id=None):
    return {
        "Id": segment_id or f"c{start}",
        "Type": "Credits",
        "Start": start,
        "End": end,
        "Source": source,
    }


def test_short_credits_before_the_real_ones_are_hidden():
    early = credits(2479.0, 2490.0)  # 11 s of music under a finale's last scene
    real = credits(2602.0, 2661.0, "BlackFrame")
    bad = markers.bad_outros([early, real], 2661.5)
    assert [(c["Id"], why) for c, why in bad] == [
        (early["Id"], "a short match well before the real credits")
    ]


def test_opening_credits_mislabelled_as_an_outro():
    bad = markers.bad_outros([credits(0.0, 100.0, "Chapter")], 6200.0)
    assert bad and "first quarter" in bad[0][1]


def test_short_credits_with_dialogue_after_them_are_hidden():
    talk = lines(
        *[(420.0 + 8 * i, 426.0 + 8 * i, f"Finn says line {i}.") for i in range(10)]
    )
    silence = []
    outro = credits(370.0, 418.0, "Unknown")
    assert markers.bad_outros([outro], 714.0, lambda a, b: talk)
    assert not markers.bad_outros([outro], 714.0, lambda a, b: silence)
    assert not markers.bad_outros(
        [outro], 714.0, lambda a, b: None
    )  # no transcript: leave it


def test_long_credits_with_a_scene_after_them_are_left_alone():
    """Anime: a 90-second ending theme, then a final scene or next-episode preview."""
    ending = credits(1233.0, 1320.0, "Combined")
    talk = lines(*[(1325.0 + 6 * i, 1330.0 + 6 * i, f"line {i}") for i in range(15)])
    assert not markers.bad_outros([ending], 1422.0, lambda a, b: talk)


def test_intro_that_is_mostly_dialogue_is_hidden_but_theme_lyrics_are_not():
    other_episodes = [
        lines(
            (30.0, 34.0, "We are the crystal gems, we'll always save the day."),
            (34.0, 38.0, "And if you think we can't, we'll always find a way."),
        )
    ]
    other = recaps.said_in(other_episodes)
    late_intro = {
        "Id": "i1",
        "Type": "Introduction",
        "Start": 153.0,
        "End": 243.0,
        "Source": "Chapter",
    }
    dialogue = lines(
        (176.0, 186.0, "So the chief wants us down at the Neibolt house by noon."),
        (
            190.0,
            200.0,
            "Nobody goes near that place after what happened to those kids.",
        ),
        (205.0, 225.0, "Then we'd better bring the shotgun and a lot more coffee."),
    )
    assert markers.bad_intros([late_intro], dialogue, other)
    theme = {
        "Id": "i2",
        "Type": "Introduction",
        "Start": 28.0,
        "End": 40.0,
        "Source": "Chapter",
    }
    lyrics = lines(
        (30.0, 34.0, "We are the crystal gems, we'll always save the day!"),
        (34.0, 36.0, "And if you think we can't,"),
        (36.0, 38.0, "we'll always find a way."),
    )
    assert not markers.bad_intros([theme], lyrics, other)
    audio_matched = {**late_intro, "Source": "Chromaprint"}
    assert not markers.bad_intros([audio_matched], dialogue, other)
    assert not markers.bad_intros([late_intro], dialogue, None)  # nothing to compare


def test_sampled_phrases_ignore_ordinary_dialogue():
    for text in (
        "Our previously watched shows are here.",
        "I'm getting married next week.",
        "The last time you saw him he was in Tokyo.",
    ):
        assert recaps.OPENER.search(text) is None
    assert recaps.OPENER.search("Previously, on Reacher...")
    assert recaps.OPENER.search("Last week on Reboot, the Ketols attacked.")
    assert markers.PREVIEW_OPENER.search("Next episode, Brave Volunteers.")
    assert not markers.PREVIEW_OPENER.search("See you next week, thanks for watching.")


def test_credits_full_of_dialogue_before_the_real_ones_are_hidden():
    dark_scene = credits(3217.0, 3287.0, "BlackFrame")
    real = credits(3324.0, 3390.0)
    other = recaps.said_in(
        [lines((10.0, 14.0, "Some line from another episode entirely."))]
    )
    talk = lines(
        *[
            (3220.0 + 7 * i, 3226.0 + 7 * i, f"Marty argues point {i} with Wendy.")
            for i in range(9)
        ]
    )
    bad = markers.bad_outros([dark_scene, real], 3392.0, lambda a, b: talk, other)
    assert [c["Id"] for c, _ in bad] == [dark_scene["Id"]]
    lyrics_only = markers.bad_outros([dark_scene, real], 3392.0, lambda a, b: [], other)
    assert lyrics_only == []


def _credits(start, end, id_="c"):
    return {"Id": id_, "Type": "Credits", "Start": start, "End": end}


def _talk(start, end, every=6.0):
    rows, t = [], start
    while t < end:
        rows.append((t, min(t + 4.5, end), f"something said at {int(t)} seconds"))
        t += every
    return lines(*rows)


def test_credits_people_keep_talking_in_start_after_the_talking():
    # A fingerprint match 7.5 minutes long; the talking stops at 20:00 and
    # the real credits follow.
    credits = _credits(842.0, 1289.0)
    speech = _talk(842.0, 1200.0)
    found = markers.talk_in_credits([credits], lambda a, b: speech)
    assert len(found) == 1
    segment, new_start, reason = found[0]
    assert segment is credits and 1199.0 < new_start < 1202.0
    assert reason == "dialogue continues to 20:00"


def test_credits_with_talking_to_the_end_are_not_credits():
    found = markers.talk_in_credits(
        [_credits(842.0, 1289.0)], lambda a, b: _talk(842.0, 1283.0)
    )
    assert found[0][1] is None and "no credits" in found[0][2]


def test_a_line_or_two_over_real_credits_moves_nothing():
    speech = lines((1240.0, 1243.0, "See you next week."), (1250.0, 1252.0, "Bye!"))
    assert (
        markers.talk_in_credits([_credits(1230.0, 1289.0)], lambda a, b: speech) == []
    )


def test_theme_lyrics_and_a_marked_preview_dont_count_as_talking():
    speech = _talk(1230.0, 1280.0)
    sung = {g for s in speech for g in recaps.grams(s.text)}
    assert (
        markers.talk_in_credits([_credits(1230.0, 1289.0)], lambda a, b: speech, sung)
        == []
    )
    assert (
        markers.talk_in_credits(
            [_credits(1230.0, 1289.0)], lambda a, b: speech, ignore=[(1225.0, 1289.0)]
        )
        == []
    )


def test_without_a_transcript_nothing_is_judged():
    assert markers.talk_in_credits([_credits(842.0, 1289.0)], None) == []
    assert markers.talk_in_credits([_credits(842.0, 1289.0)], lambda a, b: None) == []
