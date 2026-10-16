from types import SimpleNamespace

from curatarr import catchphrases


def episode(*lines, start=600):
    """One episode's sentences, away from the opening and closing minutes."""
    return [(catchphrases.words(line), start + i * 30) for i, line in enumerate(lines)]


PLOTS = [
    "Mango thieves.",
    "Volcano trouble.",
    "Lost radio.",
    "Storm night.",
    "Treasure map.",
    "Coconut phone.",
    "Rescue plane.",
    "Monkey business.",
]


def show(n, *recurring, title="Island Show"):
    """n episodes, each with its own plot and the recurring lines, at varied times."""
    return [episode(PLOTS[i], *recurring, start=400 + i * 90) for i in range(n)]


def test_longest_phrase_said_across_episodes_wins():
    episodes = show(6, "Once again immunity is back up for grabs.", "Wait a minute.")
    found = catchphrases.find(episodes, "Island Show")
    assert found[0] == ["once again immunity is back up for grabs", 6]
    # Its shorter pieces aren't listed separately.
    assert not any("immunity is back" == p for p, _ in found)


def test_once_an_episode_is_enough_but_not_a_recap_pair():
    recap = show(6, "Tell me why my mom was eaten.")[:2] + show(4)
    assert catchphrases.find(recap, "Titan Show") == []


def test_skips_the_show_name_loose_ends_and_same_spot_themes():
    named = show(6, "Hello Ricky Gervais and friends.", title="The Ricky Gervais Show")
    assert all(
        "gervais" not in p
        for p, _ in catchphrases.find(named, "The Ricky Gervais Show")
    )
    # Starts on "guys"... ends on "to": not a phrase on its own.
    assert catchphrases.find(show(6, "Guys ready to"), "Island Show") == []
    theme = [
        episode("Sunny days chasing the clouds away.", start=240) for _ in range(6)
    ]
    assert catchphrases.find(theme, "Sunny Show") == []


def test_needs_three_episodes():
    assert catchphrases.find(show(2, "Thanks a lot Gilligan."), "Isle") == []
    assert catchphrases.find(show(3, "Thanks a lot Gilligan."), "Isle") == [
        ["thanks a lot gilligan", 3]
    ]


def test_every_show_most_watched_first_and_no_everyday_english():
    titles = {k: SimpleNamespace(id=k, title=k) for k in ("a", "b", "c")}
    stored = {
        "a": {"episodes": 10, "phrases": [["thanks a lot gilligan", 9]]},
        "b": {
            "episodes": 10,
            "phrases": [["first thing in the morning", 8], ["howdy miss hathaway", 3]],
        },
        "c": {"episodes": 10, "phrases": [["brock come in brock", 4]]},
    }
    spread = {
        "titles": 100,
        "saying": {"first thing in the morning": 40, "howdy miss hathaway": 1},
    }
    picked = catchphrases.pick(stored, titles, spread, watched={"c": 5})
    assert [(p["media"].title, p["phrase"]) for p in picked] == [
        ("c", "brock come in brock"),  # watched most
        ("a", "thanks a lot gilligan"),  # then said in more of its episodes
        ("b", "howdy miss hathaway"),  # its longer phrase is everyday English
    ]
