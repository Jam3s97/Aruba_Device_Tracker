"""Every platform module sets PARALLEL_UPDATES (quality-scale rule parallel-updates)."""

from __future__ import annotations

import importlib

import pytest

from custom_components.aruba_device_tracker import AP_PLATFORMS, BASE_PLATFORMS


@pytest.mark.parametrize("platform", [p.value for p in BASE_PLATFORMS + AP_PLATFORMS])
def test_platform_sets_parallel_updates(platform):
    """Coordinator-backed platforms need no update limit, so it is 0 everywhere."""
    module = importlib.import_module(
        f"custom_components.aruba_device_tracker.{platform}"
    )

    assert module.PARALLEL_UPDATES == 0
