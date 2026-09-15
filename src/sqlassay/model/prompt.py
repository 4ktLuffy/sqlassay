"""Prompt construction.

One system prompt shared by every arm, on purpose. The arms differ only in
what retrieval put in the user message; if they also differed in their
instructions, a difference between two arms could not be attributed to
retrieval, which is the only thing the ablation is trying to measure.
"""

from __future__ import annotations

from sqlassay.retrieval.indexes import RetrievedContext

__all__ = ["SYSTEM_PROMPT", "build_user_prompt"]

SYSTEM_PROMPT = (
    "You write DuckDB SQL. Answer with a single SQL SELECT statement and nothing else: "
    "no explanation, no markdown fence, no trailing commentary. "
    "Use only the tables and columns given to you. "
    "If the question asks for an ordering, include ORDER BY."
)
"""Deliberately plain, and deliberately not tuned.

A prompt tuned against the evaluation set would make the measured score a
property of the tuning rather than of the model, and the first thing anyone
would ask of the published number is whether that happened. The place for
optimisation is a separate, declared arm -- not a quiet edit to this string.
"""


def build_user_prompt(question: str, ctx: RetrievedContext, *, hint: str = "") -> str:
    """Assemble the user message from the question and the retrieved context.

    ``hint`` is the suite's own external knowledge (BIRD's ``evidence`` field),
    supplied because BIRD's published protocol supplies it and many of its
    items are unanswerable without it. Withholding it would measure something
    other than what every published BIRD number measures.
    """
    parts: list[str] = []

    if ctx.schema_text.strip():
        header = "Full database schema:" if ctx.full_schema else "Relevant tables:"
        parts.append(f"{header}\n{ctx.schema_text}")
    else:
        # Said out loud rather than left blank. A model given no schema should
        # know it was given none, and a reader of the transcript should be
        # able to tell "retrieval returned nothing" from "schema was omitted".
        parts.append("No schema information was retrieved for this question.")

    if ctx.values:
        literals = "\n".join(
            f"- {h.payload.get('table')}.{h.payload.get('column')} = {h.payload.get('value')!r}"
            for h in ctx.values
        )
        parts.append(
            "Exact literals present in the data (use these spellings verbatim in WHERE clauses):\n"
            + literals
        )

    if ctx.examples:
        shown = "\n\n".join(
            f"Question: {h.payload.get('question', '')}\nSQL: {h.payload.get('sql', '')}"
            for h in ctx.examples
        )
        parts.append("Verified examples from this database:\n" + shown)

    if hint.strip():
        parts.append(f"External knowledge: {hint.strip()}")

    parts.append(f"Question: {question}\nSQL:")
    return "\n\n".join(parts)
