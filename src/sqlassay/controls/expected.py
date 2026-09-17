"""Loading the version-controlled expected bands.

A band cannot be quietly widened to make a run pass without the change showing
up in a diff of ``configs/expected.yaml``. That is the entire point of keeping
them in a file rather than in code.

``fireassay.controls.expected.Band`` is reused rather than redefined, and it
already refuses a band with no constraint keys -- an empty band always passes
regardless of what was observed, which is a control that cannot go red.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from fireassay.controls.expected import Band

__all__ = ["DEFAULT_EXPECTED_PATH", "load_expected"]

DEFAULT_EXPECTED_PATH = Path(__file__).resolve().parents[3] / "configs" / "expected.yaml"


def load_expected(path: Path | str = DEFAULT_EXPECTED_PATH) -> Mapping[str, Mapping[str, Band]]:
    """Load ``{control_kind: {band_name: Band}}`` from YAML.

    Comments in that file carry each band's derivation, and they are the
    reason it is YAML rather than JSON: a number without its derivation is
    a threshold someone liked.
    """
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, Band]] = {}
    for kind, bands in raw.items():
        if not isinstance(bands, dict):
            continue
        out[kind] = {name: Band(**spec) for name, spec in bands.items() if isinstance(spec, dict)}
    return out
