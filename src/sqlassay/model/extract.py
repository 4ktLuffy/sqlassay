"""Pull the SQL out of whatever the model actually emitted.

This step is itself a thing that can be wrong, and it is kept separate from
the parse gate for that reason. If extraction is broken -- a fence not
stripped, a query truncated at the first semicolon -- the parse gate reports
"the model wrote bad SQL", which is a false finding about the model. Keeping
extraction separate, tested, and recorded alongside the raw output is what
makes that distinguishable after the fact.

Deliberately conservative. It strips the three wrappers small models actually
produce, and otherwise returns the text unchanged for the parse gate to
judge. It does not try to repair SQL: a fixer here would silently improve the
model's score and the report would be measuring the fixer.
"""

from __future__ import annotations

import re

__all__ = ["extract_sql"]

_FENCE = re.compile(r"```(?:sql|SQL)?\s*\n?(?P<body>.*?)```", re.DOTALL)
_LEADING_PROSE = re.compile(r"(?is)^.*?(?=\b(?:WITH|SELECT)\b)")


def extract_sql(raw: str) -> str:
    """Return the SQL in ``raw``, or ``raw`` stripped if none is identifiable.

    Returning the input unchanged on failure is deliberate: an empty return
    would make an extraction miss look exactly like a model that produced
    nothing, and those are different defects.
    """
    if not raw or not raw.strip():
        return ""

    text = raw.strip()

    # 1. A fenced block, if there is one. First block only: a model that
    #    emits two has not given one answer, and picking one would be a guess.
    m = _FENCE.search(text)
    if m:
        text = m.group("body").strip()

    # 2. Prose before the query ("Sure! Here's the SQL: SELECT ..."). Only cut
    #    when a SELECT/WITH actually follows, so text with neither is passed
    #    through whole and fails the parse gate honestly.
    if not re.match(r"(?is)^\s*(?:WITH|SELECT)\b", text):
        trimmed = _LEADING_PROSE.sub("", text, count=1)
        if trimmed.strip():
            text = trimmed.strip()

    # 3. One trailing semicolon and any trailing prose after it. Anything
    #    beyond the first statement is dropped here rather than silently
    #    executed -- and the parse gate independently refuses stacked
    #    statements, so a miss here cannot become a second statement running.
    if ";" in text:
        text = text.split(";", 1)[0]

    return text.strip()
