from __future__ import annotations

from assay_watch_mcp.catalog import Reference, find_reference

_CATALOG = [
    Reference(
        ref="126610LN",
        brand="Rolex",
        model_name="Submariner Date",
        search_aliases=["126610LN", "126610 LN"],
    ),
    Reference(
        ref="126710BLRO",
        brand="Rolex",
        model_name="GMT-Master II Pepsi",
        search_aliases=["126710BLRO"],
    ),
    Reference(
        ref="M79030N",
        brand="Tudor",
        model_name="Black Bay Fifty-Eight",
        search_aliases=["M79030N", "79030N"],
    ),
]


def test_exact_reference_number_match() -> None:
    matches = find_reference("looking for a 126610LN", _CATALOG)
    assert matches[0].ref == "126610LN"
    assert matches[0].confidence == "exact"


def test_exact_alias_match_case_insensitive() -> None:
    matches = find_reference("any m79030n in stock?", _CATALOG)
    assert matches[0].ref == "M79030N"
    assert matches[0].confidence == "exact"


def test_brand_and_model_word_overlap() -> None:
    matches = find_reference("Rolex Submariner", _CATALOG)
    assert matches[0].ref == "126610LN"
    assert matches[0].confidence in ("likely", "possible")


def test_no_match_returns_empty_list() -> None:
    assert find_reference("a Casio G-Shock", _CATALOG) == []


def test_shared_brand_word_alone_does_not_match_unrelated_model() -> None:
    """Caught live: "Rolex Submariner" was also matching the Daytona at
    "likely" confidence purely because they share the brand word "Rolex" --
    brand alone is never distinguishing, every Rolex reference shares it."""
    catalog = [
        *_CATALOG,
        Reference(ref="116500LN", brand="Rolex", model_name="Daytona", search_aliases=[]),
    ]
    matches = find_reference("Rolex Submariner", catalog)
    assert [m.ref for m in matches] == ["126610LN"]  # Daytona must not appear at all


def test_marketing_nickname_alone_does_not_exact_match() -> None:
    """ "Fifty-Eight" was deliberately removed from M79030N's aliases (it
    also names the discontinued non-M-prefix generation) -- this must not
    silently resurrect that collision via the matcher."""
    matches = find_reference("Fifty-Eight", _CATALOG)
    assert all(m.confidence != "exact" for m in matches)
