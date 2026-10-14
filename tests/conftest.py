"""Fixtures shared by the gateway test modules."""

from __future__ import annotations

import pytest
from gateway_harness import MACHINE, OTHER_MACHINE, FakeMachine, Harness, _default_registry, _machines_file, _write_json


@pytest.fixture
def gw(tmp_path) -> Harness:
    h = Harness(
        tmp=tmp_path,
        machine=FakeMachine(),
        registry_path=tmp_path / "cfg" / "registry.json",
        machines_path=tmp_path / "claude" / "remoteos-mcp.mcp.json",
        access_log_path=tmp_path / "state" / "access.jsonl",
    )
    _write_json(h.registry_path, _default_registry())
    _machines_file(h.machines_path, [MACHINE, OTHER_MACHINE])
    return h
