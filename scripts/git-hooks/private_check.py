#!/usr/bin/env python3
"""Stops a commit that would publish personal data (AGENTS.md).

Turned on per checkout with ``git config core.hooksPath scripts/git-hooks``.
The words to refuse (your household's names, server names, network paths,
addresses) can't live in this public repository, so they're read from a file
of your own: ``~/.config/curatarr-dev/private-words``, one per line, matched
as whole words with their capitals as written (so "Sam" is refused but
"sample" isn't). Lines starting with # are notes.

The pre-commit hook checks the lines being added and the file names, and that
the author's email is GitHub's private address; the commit-msg hook checks
the message, and refuses AI co-author and session lines.
"""

import re
import subprocess
import sys
from pathlib import Path

WORDS = Path.home() / ".config" / "curatarr-dev" / "private-words"
PRIVATE_EMAIL = "users.noreply.github.com"
# Split so the repository's own check doesn't find these in this file.
AI_LINES = re.compile(
    "Co-Authored-By|Claude-" + "Session|claude\\.ai/" + "code", re.IGNORECASE
)


def private_words() -> list[str]:
    if not WORDS.exists():
        return []
    lines = (line.strip() for line in WORDS.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def found_in(text: str, words: list[str]) -> list[str]:
    return sorted(
        {word for word in words if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text)}
    )


def _git(*args) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    ).stdout


def check_staged(words: list[str]) -> list[str]:
    problems = []
    email = _git("config", "user.email").strip()
    if not email.endswith(PRIVATE_EMAIL):
        problems.append(
            f"the author's email ({email or 'none'}) isn't GitHub's private "
            f"address (…@{PRIVATE_EMAIL})"
        )
    diff = _git("diff", "--cached", "-U0", "--no-color")
    added = "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    names = _git("diff", "--cached", "--name-only")
    words_found = found_in(added + "\n" + names, words)
    if words_found:
        problems.append("the changes contain " + ", ".join(words_found))
    return problems


def check_message(text: str, words: list[str]) -> list[str]:
    text = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    problems = []
    words_found = found_in(text, words)
    if words_found:
        problems.append("the message contains " + ", ".join(words_found))
    if AI_LINES.search(text):
        problems.append("the message has an AI co-author or session line")
    return problems


def main(argv: list[str]) -> int:
    words = private_words()
    if argv[1] == "pre-commit":
        problems = check_staged(words)
    else:
        problems = check_message(Path(argv[2]).read_text(encoding="utf-8"), words)
    if not words:
        print(
            f"Note: no private word list at {WORDS}; only the generic checks ran.",
            file=sys.stderr,
        )
    if problems:
        print("Commit refused: " + "; ".join(problems) + ".", file=sys.stderr)
        print(
            "Use made-up names (Alex, Sam, Pat) and generic paths; see AGENTS.md.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
