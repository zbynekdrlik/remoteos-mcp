"""The tool list a client gets, pinned (#49).

``tests/golden/tools_list.json`` was written from the unsplit ``__main__.py``, before any move:
every tool's name, description, schemas and annotations, in order. Splitting or moving tool code
must leave this reply byte-identical. A deliberate change to a tool regenerates the file:

    PYTHONPATH=src:tests python tests/test_tools_list_golden.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from fleet_harness import hermetic_child_env

GOLDEN = Path(__file__).parent / "golden" / "tools_list.json"

# A fresh interpreter: the start-up tests' tool filter changes the in-process server's list.
_DUMP = """
import asyncio, json
from fastmcp import Client
import remoteos.__main__ as server

async def main():
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    print(json.dumps([t.model_dump(mode="json", by_alias=True) for t in tools], indent=1, sort_keys=True))

asyncio.run(main())
"""


def live_tools_list(home: Path) -> list[dict]:
    result = subprocess.run(
        [sys.executable, "-c", _DUMP],
        env=hermetic_child_env(home),
        cwd=home,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_tools_list_matches_the_golden_snapshot(tmp_path):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    live = live_tools_list(tmp_path)
    assert [t["name"] for t in live] == [t["name"] for t in golden]  # the readable failure first
    assert live == golden


def test_the_snapshot_covers_every_tool_with_its_contract():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert len(golden) == 47
    assert all(t["description"] and t["inputSchema"] and t["annotations"] for t in golden)
    assert golden[0]["name"] == "Snapshot" and golden[-1]["name"] == "GetRunningTasks"
    shell = next(t for t in golden if t["name"] == "Shell")
    assert shell["outputSchema"]["required"] == ["stdout", "stderr", "exit_code"]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as home:
        GOLDEN.write_text(json.dumps(live_tools_list(Path(home)), indent=1, sort_keys=True) + "\n", encoding="utf-8")
