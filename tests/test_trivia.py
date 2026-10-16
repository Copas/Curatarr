import json
from types import SimpleNamespace

from curatarr import db, enrichment, trivia
from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart


def said(*lines, every=10.0, start=0.0, end=None):
    """Segments every `every` seconds from `start`; `end` adds a last (credits)
    segment there, so the lines are mid-episode."""
    segments = [
        SimpleNamespace(start=start + i * every, end=start + i * every + 4, text=text)
        for i, text in enumerate(lines)
    ]
    if end is not None:
        segments.append(SimpleNamespace(start=end - 4, end=end, text="Credits."))
    return segments


def test_count_tallies_swearing_violence_love_and_manners():
    stats = trivia.count(
        said(
            "What the fuck? Shit, he's dead.",
            "I love you. Please, thank you.",
            "Help! Run! Oh my god, get out!",
            "Fucking hell, where's the gun?",
        )
    )
    assert (stats["f_words"], stats["swears"]) == (2, 3)
    assert stats["violent"] == 2  # dead, gun
    assert (stats["love"], stats["polite"], stats["panic"]) == (1, 2, 4)
    assert stats["questions"] == 2 and stats["words"] == 24


def _transcribed(
    app, title, kind, minutes, lines, *, library, episode=1, start=600.0, language="en"
):
    media = db.session.query(MediaIdentity).filter_by(
        title=title
    ).first() or MediaIdentity(
        library_id=library.id,
        media_type="movie" if kind == "movie_file" else "series",
        title=title,
    )
    db.session.add(media)
    db.session.flush()
    part = MediaPart(
        media_identity_id=media.id,
        kind=kind,
        season_number=None if kind == "movie_file" else 1,
        episode_number=None if kind == "movie_file" else episode,
        has_file=True,
        file_path=f"/media/{title}/{episode}.mkv",
    )
    db.session.add(part)
    db.session.flush()
    key = f"{abs(hash((title, episode))):032x}"[:32]
    folder = enrichment.cache_dir() / "transcripts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{key}.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"start": s.start, "end": s.end, "text": s.text}
                    for s in said(*lines, start=start, end=minutes * 60)
                ],
                "details": {"language": language},
            }
        )
    )
    state = enrichment.state_for(part)
    state.transcript_key, state.duration_seconds = key, minutes * 60
    db.session.commit()
    return state


def test_the_fun_facts_page_ranks_films_and_shows(app, client):
    films = Library(jellyfin_library_id="m", name="Films", media_type="movies")
    tv = Library(jellyfin_library_id="t", name="TV", media_type="tv")
    db.session.add_all([films, tv])
    db.session.flush()
    _transcribed(
        app, "Sweary Film", "movie_file", 90, ["Fuck this shit."] * 50, library=films
    )
    _transcribed(
        app,
        "Polite Film",
        "movie_file",
        90,
        ["Please, thank you kindly."] * 50,
        library=films,
    )
    for n in range(1, 8):
        _transcribed(
            app,
            "Prank Show",
            "episode",
            22,
            # A different ending each episode: only "the other guys" recurs.
            [f"The other guys {verb} again." for verb in ("lose", "fail")]
            + [f"Then {n} tried."] * 3,
            library=tv,
            episode=n,
            start=300 + n * 97,  # said at a different point each episode
        )
        # A theme after the cold open: same words, same spot, every episode.
        _transcribed(
            app,
            "Theme Show",
            "episode",
            22,
            ["Sunny days chasing the clouds away"] * 2 + [f"Plot {n}."] * 3,
            library=tv,
            episode=n,
            start=240,
        )
        # The show's own name ("Ricky Gervais, Stephen Merchant").
        _transcribed(
            app,
            "The Ricky Gervais Show",
            "episode",
            22,
            ["Hello Ricky Gervais"] * 2 + [f"Story {n}."] * 3,
            library=tv,
            episode=n,
            start=300 + n * 97,
        )
    # Transcribed in another language (a misfiled foreign show): every count
    # looks for English words, so it would be the "cleanest" and "quietest".
    _transcribed(
        app,
        "Hong Kong Show",
        "episode",
        45,
        ["少爺, 去哪兒了?"] * 9,
        library=tv,
        language="zh",
    )
    for n in (2, 3):
        _transcribed(
            app,
            "Hong Kong Show",
            "episode",
            45,
            ["阿樂"] * 9,
            library=tv,
            episode=n,
            language="zh",
        )
    assert trivia.sweep() == 26 and trivia.sweep() == 0  # each counted once
    from curatarr import catchphrases

    assert catchphrases.sweep() and catchphrases.sweep() == 0  # until it changes
    facts = trivia.facts()
    swearing = next(c for c in facts["categories"] if c["id"] == "swearing")
    assert swearing["films"][0]["media"].title == "Sweary Film"
    clean = next(c for c in facts["categories"] if c["id"] == "clean")
    assert [f["media"].title for f in clean["films"]] == ["Polite Film"]
    assert "Hong Kong Show" not in {s["media"].title for s in clean["shows"]}
    # Not the theme (same spot every episode), the show's own name, or a show
    # transcribed in another language.
    assert [(c["media"].title, c["phrase"]) for c in facts["catchphrases"]] == [
        ("Prank Show", "other guys fail again")
    ]
    page = client.get("/fun").text
    assert "Potty mouths" in page and "Sweary Film" in page
    assert "“other guys fail again”" in page and "in 7 of 7 episodes" in page
    assert "3 transcripts in other languages are left out" in page
    # A replaced transcript is counted again.
    state = db.session.query(ChapterState).first()
    state.transcript_key = "0" * 32
    db.session.commit()
    assert trivia.sweep() == 1


def test_the_page_explains_itself_before_anything_is_transcribed(client):
    assert "Nothing counted yet" in client.get("/fun").text


def test_every_ranking_is_a_rate(app, client):
    """A show with hundreds of episodes mustn't win by length (The Simpsons and
    its 177 "I love you"s)."""
    tv = Library(jellyfin_library_id="t", name="TV", media_type="tv")
    db.session.add(tv)
    db.session.flush()
    for n in range(1, 21):  # long-running, says it now and then
        _transcribed(
            app,
            "Long Show",
            "episode",
            22,
            ["I love you."] + ["Hi."] * 30,
            library=tv,
            episode=n,
        )
    for n in range(1, 4):  # short-lived, says it all the time
        _transcribed(
            app,
            "Sweet Show",
            "episode",
            22,
            ["I love you."] * 10,
            library=tv,
            episode=n,
        )
    trivia.sweep()
    facts = trivia.facts()
    for category in facts["categories"]:
        assert "hour" in category["unit"] or "minute" in category["unit"], category[
            "id"
        ]
    love = next(c for c in facts["categories"] if c["id"] == "love")
    assert love["shows"][0]["media"].title == "Sweet Show"
    page = client.get("/fun").text
    assert "Swears an hour" in page and "I love you” an hour" in page
    assert '"I love you"s' not in page and "“I love you”s" not in page


def test_names_pauses_vocabulary_and_exclamations():
    lines = [
        SimpleNamespace(start=300, end=304, text="Well, Michael said no. Ask Michael!"),
        SimpleNamespace(start=305, end=309, text="Your Honor, Sister Bertrille left!"),
        SimpleNamespace(start=310, end=314, text="We flew to New York with Dwight."),
        # Speech recognition stretched nine words over 18 minutes of quiet.
        SimpleNamespace(
            start=320, end=1400, text="I don't know what to say to you now."
        ),
        SimpleNamespace(start=1400, end=1403, text="Then Pam left."),
        SimpleNamespace(start=3000, end=3004, text="Credits roll now."),
    ]
    stats = trivia.count(lines)
    assert stats["first_word"] == 300 and stats["exclaims"] == 2
    # Sentence-initial words, titles and place words aren't names.
    assert stats["names"] == {"Michael": 2, "Bertrille": 1, "Dwight": 1, "Pam": 1}
    # The stretched line is quiet after its words, not 18 minutes of talking.
    assert stats["longest_talk"] < 60
    assert stats["longest_silence"] > 1000


def test_vocabulary_is_per_thousand_words():
    import itertools

    letters = "abcdefghij"
    varied = " ".join(
        "".join(t) for t in itertools.islice(itertools.product(letters, repeat=4), 2000)
    )
    stats = trivia.count([SimpleNamespace(start=0, end=600, text=varied)])
    assert stats["vocab_windows"] == 2 and stats["vocab_sum"] == 2000
    samey = " ".join(["same thing"] * 1000)
    stats = trivia.count([SimpleNamespace(start=0, end=600, text=samey)])
    assert stats["vocab_sum"] / stats["vocab_windows"] == 2


def test_who_gets_talked_about(app, client):
    tv = Library(jellyfin_library_id="t", name="TV", media_type="tv")
    db.session.add(tv)
    db.session.flush()
    for n in range(1, 4):
        _transcribed(
            app,
            "Gilligan's Island",
            "episode",
            25,
            ["Hello, Gilligan, where is the Skipper?", "Ask the Skipper now."] * 3
            + [f"Guest star Thurston{n} waves."],
            library=tv,
            episode=n,
        )
    trivia.sweep()
    names = trivia.facts()["names"]
    # Not the show's own name, not a one-episode guest.
    assert [(n["media"].title, n["name"]) for n in names] == [
        ("Gilligan's Island", "Skipper")
    ]
    page = client.get("/fun").text
    assert "Who gets talked about" in page and "“Skipper”" in page
    # Vocabulary needs 5,000 words a show, more than these episodes have.
    assert "Shouty" in page and "Slow burners" in page and "Non-stop talkers" in page
