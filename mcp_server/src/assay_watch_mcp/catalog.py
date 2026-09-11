"""The tracked-reference catalog and free-text matching against it.

Deliberately narrow scope: this resolves "which of our 17 tracked references
does the caller mean" from brand/model/reference-number signals only. It does
not parse budget, condition, or material constraints -- that belongs one
layer up, wherever the caller (an LLM, a form) interprets the user's full
request, not in reference identification itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}


class Reference(BaseModel):
    ref: str
    brand: str
    model_name: str
    search_aliases: list[str] = Field(default_factory=list)
    enabled: bool = True


class ReferenceMatch(BaseModel):
    ref: str
    brand: str
    model_name: str
    confidence: Literal["exact", "likely", "possible"]


def load_catalog(path: Path) -> list[Reference]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Reference(**r) for r in data.get("references", []) if r.get("enabled", True)]


def find_reference(query: str, catalog: list[Reference], *, limit: int = 5) -> list[ReferenceMatch]:
    """Score every tracked reference against ``query`` and return the best
    matches, most confident first. Never raises on a query that matches
    nothing -- an empty list is a legitimate answer."""
    normalized_query = _normalize(query)
    query_words = _words(query)
    scored: list[tuple[float, ReferenceMatch]] = []

    for reference in catalog:
        exact_terms = [reference.ref, *reference.search_aliases]
        if any(_normalize(term) and _normalize(term) in normalized_query for term in exact_terms):
            scored.append(
                (
                    100.0,
                    ReferenceMatch(
                        ref=reference.ref,
                        brand=reference.brand,
                        model_name=reference.model_name,
                        confidence="exact",
                    ),
                )
            )
            continue

        catalog_words = _words(reference.brand) | _words(reference.model_name)
        if not catalog_words or not query_words:
            continue
        overlap = len(catalog_words & query_words) / len(catalog_words)
        if overlap <= 0:
            continue
        confidence: Literal["likely", "possible"] = "likely" if overlap >= 0.5 else "possible"
        scored.append(
            (
                overlap * 10,
                ReferenceMatch(
                    ref=reference.ref,
                    brand=reference.brand,
                    model_name=reference.model_name,
                    confidence=confidence,
                ),
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [match for _, match in scored[:limit]]
