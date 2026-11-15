"""The stylesheets parse: a stray or missing brace makes browsers drop the
rule after it without a word (a leftover "}" once cost Generate's cast picker
its grid, so the photos showed page-wide)."""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "curatarr" / "static"


@pytest.mark.parametrize("sheet", sorted(STATIC.glob("*.css")), ids=lambda p: p.name)
def test_braces_balance(sheet):
    text = re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.DOTALL)
    text = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", "", text)  # strings may hold braces
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        for char in line:
            depth += char == "{"
            depth -= char == "}"
            assert depth >= 0, (
                f"{sheet.name}: a closing brace with no rule, line {number}"
            )
    assert depth == 0, f"{sheet.name}: {depth} rule(s) never closed"
