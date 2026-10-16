import json
from types import SimpleNamespace

from curatarr import db, enrichment, trivia
from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart


def said(*lines, every=10.0):
    return [
        SimpleNamespace(start=i * every, end=i * every + 4, text=text)
        for i, text in enumerate(lines)
    ]


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


def test_catchphrases_skip_filler_and_common_words():
    lines = ["Ha ha ha, the other guys lose."] * 3 + ["What are you talking about?"] * 3
    assert trivia.count(said(*lines))["phrases"] == [
        "the other guys",
        "other guys lose",
    ]


def _transcribed(app, title, kind, minutes, lines, *, library, episode=1):
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
                    for s in said(*lines)
                ]
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
        )
    assert trivia.sweep() == 9 and trivia.sweep() == 0  # each counted once
    facts = trivia.facts()
    swearing = next(c for c in facts["categories"] if c["id"] == "swearing")
    assert swearing["films"][0]["media"].title == "Sweary Film"
    clean = next(c for c in facts["categories"] if c["id"] == "clean")
    assert [f["media"].title for f in clean["films"]] == ["Polite Film"]
    assert facts["catchphrases"][0]["phrase"] == "the other guys"
    page = client.get("/fun").text
    assert "Potty mouths" in page and "Sweary Film" in page
    assert "“the other guys”" in page and "in 7 of 7 episodes" in page
    # A replaced transcript is counted again.
    state = db.session.query(ChapterState).first()
    state.transcript_key = "0" * 32
    db.session.commit()
    assert trivia.sweep() == 1


def test_the_page_explains_itself_before_anything_is_transcribed(client):
    assert "Nothing counted yet" in client.get("/fun").text
