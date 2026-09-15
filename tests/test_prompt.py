"""Prompt assembly tests.

The arms must differ only in what retrieval contributed. If they also differed
in their instructions, a difference between two arms could not be attributed
to retrieval -- which is the only thing the ablation measures.
"""

from __future__ import annotations

from sqlassay.model.prompt import SYSTEM_PROMPT, build_user_prompt
from sqlassay.retrieval.indexes import RetrievedContext
from sqlassay.retrieval.store import Hit


def ctx(**kw: object) -> RetrievedContext:
    base: dict[str, object] = {"arm": "schema", "schema_text": "Table t (a INTEGER)"}
    base.update(kw)
    return RetrievedContext(**base)  # type: ignore[arg-type]


class TestUserPrompt:
    def test_question_is_always_present(self) -> None:
        assert "How many rows?" in build_user_prompt("How many rows?", ctx())

    def test_schema_is_included(self) -> None:
        assert "Table t (a INTEGER)" in build_user_prompt("q", ctx())

    def test_full_schema_is_labelled_differently(self) -> None:
        full = build_user_prompt("q", ctx(full_schema=True))
        retrieved = build_user_prompt("q", ctx(full_schema=False))
        assert "Full database schema:" in full
        assert "Relevant tables:" in retrieved

    def test_missing_schema_is_stated_not_silent(self) -> None:
        # A reader must be able to tell "retrieval returned nothing" from
        # "schema was omitted", and so must the model.
        assert "No schema information was retrieved" in build_user_prompt("q", ctx(schema_text=""))

    def test_value_literals_are_quoted_for_verbatim_use(self) -> None:
        hit = Hit("v1", "ACME", {"table": "customers", "column": "name", "value": "ACME CORP"}, 0.1)
        out = build_user_prompt("orders from Acme", ctx(values=(hit,)))
        assert "'ACME CORP'" in out
        assert "customers.name" in out

    def test_examples_show_question_and_sql(self) -> None:
        hit = Hit("e1", "", {"question": "how many?", "sql": "SELECT COUNT(*) FROM t"}, 0.1)
        out = build_user_prompt("q", ctx(examples=(hit,)))
        assert "how many?" in out
        assert "SELECT COUNT(*) FROM t" in out

    def test_absent_components_add_nothing(self) -> None:
        bare = build_user_prompt("q", ctx())
        assert "Verified examples" not in bare
        assert "Exact literals" not in bare


class TestSystemPrompt:
    def test_is_shared_and_constant_across_arms(self) -> None:
        # Pinned so that a future edit to make one arm look better has to be
        # a visible change to a shared constant, not a quiet per-arm tweak.
        assert "single SQL SELECT statement" in SYSTEM_PROMPT
        assert "ORDER BY" in SYSTEM_PROMPT
