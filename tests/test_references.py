from __future__ import annotations

from pathlib import Path

from assay_watch.references import load_enabled_references, load_references

_YAML = """
references:
  - ref: "A"
    brand: "X"
    model_name: "one"
    search_aliases: ["a1", "a2"]
    enabled: true
  - ref: "B"
    brand: "Y"
    model_name: "two"
    enabled: false
"""


def test_load_all_and_enabled(tmp_path: Path) -> None:
    path = tmp_path / "refs.yaml"
    path.write_text(_YAML)

    all_refs = load_references(path)
    assert {r.ref for r in all_refs} == {"A", "B"}

    enabled = load_enabled_references(path)
    assert [r.ref for r in enabled] == ["A"]
    assert enabled[0].all_terms() == ["A", "a1", "a2"]
