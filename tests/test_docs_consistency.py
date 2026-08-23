"""README.md and docs/index.md must not drift apart.

docs/index.md is a hand-maintained near-copy of README.md: same tables, same
measured numbers, a landing-page intro and a `See also` section of its own.
That is two edit sites for every benchmark re-run -- exactly the duplication
trap this project has hit before -- and the two had already begun to diverge
in structure when it was spotted.

Rather than generate one file from the other (which would replace the docs
landing page with the README verbatim, losing its intro and See-also), this
pins the part that actually matters and that a human reliably forgets: the
*facts*. Every markdown table row, and every headline number in the prose,
must appear in both files. Prose wording, heading levels, section separators
and the intro stay free to differ.

If this fails after a deliberate change: update the number in both files.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"
_DOCS_INDEX = _ROOT / "docs" / "index.md"


def _table_rows(text: str) -> list[tuple[str, ...]]:
    """Every markdown table row, cells stripped, separator rows dropped."""
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = tuple(c.strip() for c in stripped.strip("|").split("|"))
        if all(set(c) <= set("-: ") for c in cells):
            continue  # the |---|---| separator
        rows.append(cells)
    return rows


# "~28x", "2.5x", "1,281" -- the shapes the measured claims take.
# U+00D7 MULTIPLICATION SIGN is accepted alongside a latin "x", so a speed-up
# written with one character in one file and the other character in the other
# still compares as the same claim. It is spelled chr(0xD7) rather than
# inlined because the two are indistinguishable on sight in source.
_MULTIPLICATION_SIGN = chr(0xD7)
# The \b belongs to the latin-x branch only: U+00D7 is not a word character,
# so a trailing \b after it would never match ("40<mult> " has no boundary there).
_NUMBER = re.compile(
    r"~?\d[\d,]*(?:\.\d+)?\s*(?:x\b|" + _MULTIPLICATION_SIGN + r")|\b\d{1,3}(?:,\d{3})+\b"
)


def _prose_numbers(text: str) -> set[str]:
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("|"))
    return {m.group(0).replace(" ", "") for m in _NUMBER.finditer(body)}


def test_both_files_exist() -> None:
    assert _README.is_file()
    assert _DOCS_INDEX.is_file()


def test_table_rows_are_identical() -> None:
    readme = _table_rows(_README.read_text(encoding="utf-8"))
    docs = _table_rows(_DOCS_INDEX.read_text(encoding="utf-8"))

    only_readme = sorted(set(readme) - set(docs))
    only_docs = sorted(set(docs) - set(readme))
    assert not only_readme, f"table rows in README.md but not docs/index.md: {only_readme}"
    assert not only_docs, f"table rows in docs/index.md but not README.md: {only_docs}"
    assert len(readme) == len(docs), "the same rows appear a different number of times in each file"


def test_headline_numbers_are_identical() -> None:
    readme = _prose_numbers(_README.read_text(encoding="utf-8"))
    docs = _prose_numbers(_DOCS_INDEX.read_text(encoding="utf-8"))

    assert not readme - docs, f"numbers in README.md but not docs/index.md: {sorted(readme - docs)}"
    assert not docs - readme, f"numbers in docs/index.md but not README.md: {sorted(docs - readme)}"
