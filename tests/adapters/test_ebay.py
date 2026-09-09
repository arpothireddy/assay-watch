from __future__ import annotations

import pytest

from assay_watch.adapters.base import HealthStatus, Reference
from assay_watch.adapters.ebay import EbayAdapter


def test_unconfigured_without_credentials() -> None:
    adapter = EbayAdapter(client_id="", client_secret="")
    assert adapter.health_check().status is HealthStatus.UNCONFIGURED


def test_ok_with_credentials() -> None:
    adapter = EbayAdapter(client_id="id", client_secret="secret")
    assert adapter.health_check().status is HealthStatus.OK


def test_fetch_not_implemented() -> None:
    adapter = EbayAdapter(client_id="id", client_secret="secret")
    ref = Reference(ref="126610LN", brand="Rolex", model_name="Submariner")
    with pytest.raises(NotImplementedError):
        adapter.fetch(ref)
