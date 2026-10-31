"""The local commit check (scripts/git-hooks/private_check.py)."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/git-hooks/private_check.py"
spec = importlib.util.spec_from_file_location("private_check", SCRIPT)
private_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(private_check)


def test_words_match_whole_and_with_their_capitals():
    words = ["Sam", "nas01", "/mnt/family"]
    assert private_check.found_in("Photos of Sam on nas01", words) == ["Sam", "nas01"]
    assert private_check.found_in("a sample, Samuel, sam", words) == []
    assert private_check.found_in("under /mnt/family/films", words) == ["/mnt/family"]


def test_messages_with_names_or_ai_lines_are_refused():
    assert private_check.check_message("Fix the poster for Sam", ["Sam"]) == [
        "the message contains Sam"
    ]
    assert private_check.check_message(
        "Fix it\n\nCo-Authored-By: Someone <x@example.com>", []
    ) == ["the message has an AI co-author or session line"]
    assert private_check.check_message("# Sam\nFix the poster", ["Sam"]) == []


def test_the_word_list_is_read_from_outside_the_repository(tmp_path, monkeypatch):
    listed = tmp_path / "private-words"
    listed.write_text("# names\nSam\n\nnas01\n", encoding="utf-8")
    monkeypatch.setattr(private_check, "WORDS", listed)
    assert private_check.private_words() == ["Sam", "nas01"]
    assert not str(private_check.WORDS).startswith(str(SCRIPT.parent.parent))
