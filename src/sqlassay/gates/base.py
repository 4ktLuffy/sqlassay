"""The Gate protocol.

A gate is one deterministic check on a prediction. Two rules, both taken from
``agent-assay``'s scorer contract and both load-bearing:

1. **A check that could not run must never read as a check that passed.**
   ``GateStatus`` is ``PASSED`` / ``FAILED`` / ``NOT_RUN``, and a downstream
   gate skipped because an upstream one failed reports ``NOT_RUN`` with the
   reason. It is not a failure; nothing was measured.

2. **Every gate must be able to prove it still goes red.** ``selftest`` runs
   the gate against planted defects and returns ``(ok, detail)``. A gate with
   no way to demonstrate failure is not evidence, and the ``selftest`` command
   refuses to report a run when any gate's probes do not fire.

``selftest`` returns rather than raises so that one broken gate does not stop
the other two being checked.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlassay.models import GateResult

__all__ = ["Gate", "GateContext"]


class GateContext(Protocol):
    """What a gate may read beyond the prediction it is judging.

    A Protocol rather than a dataclass because the three gates need
    disjoint things -- the parse gate needs only a dialect, the execute gate
    needs a database, the resultset gate needs the gold rows -- and a single
    struct carrying all of it would let any gate reach anything.
    """

    dialect: str


@runtime_checkable
class Gate(Protocol):
    """One deterministic check on one prediction."""

    name: str
    version: str

    def selftest(self) -> tuple[bool, str]:
        """Run this gate's planted-defect probes; return ``(ok, detail)``.

        ``detail`` names the probes that ran when ``ok``, and the first probe
        that failed to fire when not -- so a failure says *which* check went
        blind, not merely that something did.
        """
        ...


def skipped(name: str, version: str, because: str) -> GateResult:
    """A ``NOT_RUN`` result for a gate an upstream failure stopped.

    Centralised so that no gate can express "did not run" as a quiet
    ``FAILED``, and so the reason is always recorded.
    """
    from sqlassay.models import GateStatus

    return GateResult(
        gate=name,
        version=version,
        status=GateStatus.NOT_RUN,
        evidence=f"not run: {because}",
    )
