"""Loading the canonical reference registry from ``config/references.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .adapters.base import Reference


def load_references(path: Path) -> list[Reference]:
    """Load all references (enabled and disabled) from the YAML registry."""
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Reference(**entry) for entry in data.get("references", [])]


def load_enabled_references(path: Path) -> list[Reference]:
    """Load only the references marked ``enabled: true``."""
    return [ref for ref in load_references(path) if ref.enabled]
