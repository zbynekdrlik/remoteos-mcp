"""FileUpload(append), FileHash, FileRename and Shell's structured content (#67), on real files."""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from fastmcp import Client
from tool_test_support import tool_error

import remoteos.__main__ as server
from remoteos import file_transfer
from remoteos.tiers import TOOL_TIERS


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _call(tool: str, arguments: dict):
    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(go())


def test_append_adds_to_the_file_and_the_default_replaces_it(tmp_path):
    dest = tmp_path / "sub" / "f.bin"
    assert server.FileUpload(str(dest), _b64(b"one")) == f"Written 3 bytes to {dest}"
    assert server.FileUpload(str(dest), _b64(b"two"), append=True) == f"Written 3 bytes to {dest}"
    assert dest.read_bytes() == b"onetwo"
    server.FileUpload(str(dest), _b64(b"new"))
    assert dest.read_bytes() == b"new"


def test_append_creates_a_missing_file(tmp_path):
    dest = tmp_path / "new" / "f.bin"
    server.FileUpload(str(dest), _b64(b"x"), append=True)
    assert dest.read_bytes() == b"x"


def test_bad_base64_with_append_changes_nothing(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"keep")
    tool_error(lambda: server.FileUpload(str(dest), "!!!!", append=True))
    assert dest.read_bytes() == b"keep"


def test_file_hash_reports_the_sha256_and_the_size_across_read_pieces(tmp_path, monkeypatch):
    monkeypatch.setattr(file_transfer, "_READ_BYTES", 7)
    path = tmp_path / "f.bin"
    payload = bytes(range(256)) * 3
    path.write_bytes(payload)
    assert server.FileHash(str(path)) == f"sha256:{hashlib.sha256(payload).hexdigest()} {len(payload)} bytes"


def test_file_hash_of_a_missing_file_and_of_a_directory_are_errors(tmp_path):
    assert tool_error(lambda: server.FileHash(str(tmp_path / "no"))) == f"File not found: {tmp_path / 'no'}"
    assert tool_error(lambda: server.FileHash(str(tmp_path))).startswith("FileHash error: ")


def test_rename_moves_the_file_to_a_new_name(tmp_path):
    source, target = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"data")
    assert server.FileRename(str(source), str(target)) == f"Renamed {source} to {target}"
    assert target.read_bytes() == b"data"
    assert not source.exists()


def test_rename_over_an_existing_file_keeps_its_mode(tmp_path):
    source, target = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"new")
    source.chmod(0o644)
    target.write_bytes(b"old")
    target.chmod(0o600)
    server.FileRename(str(source), str(target))
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o600


def test_rename_to_a_new_name_keeps_the_source_mode(tmp_path):
    source, target = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"new")
    source.chmod(0o640)
    server.FileRename(str(source), str(target))
    assert target.stat().st_mode & 0o777 == 0o640


def test_rename_as_root_keeps_the_owner_of_the_existing_file(tmp_path, monkeypatch):
    source, target = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    owner = (target.stat().st_uid, target.stat().st_gid)
    owners = []
    monkeypatch.setattr(file_transfer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(file_transfer.os, "chown", lambda path, uid, gid: owners.append((path, uid, gid)))
    server.FileRename(str(source), str(target))
    assert owners == [(source, *owner)]


def test_rename_writes_through_a_symlink(tmp_path):
    source, real, link = tmp_path / "a", tmp_path / "real", tmp_path / "link"
    source.write_bytes(b"new")
    real.write_bytes(b"old")
    link.symlink_to(real)
    server.FileRename(str(source), str(link))
    assert link.is_symlink()
    assert real.read_bytes() == b"new"


def test_rename_refuses_a_missing_source_and_a_directory_target(tmp_path):
    assert tool_error(lambda: server.FileRename(str(tmp_path / "no"), str(tmp_path / "b"))) == (
        f"File not found: {tmp_path / 'no'}"
    )
    source = tmp_path / "a"
    source.write_bytes(b"x")
    out = tool_error(lambda: server.FileRename(str(source), str(tmp_path)))
    assert out == f"FileRename error: {tmp_path} is a directory"
    assert source.read_bytes() == b"x"


def test_rename_syncs_the_data_before_the_name_changes(tmp_path, monkeypatch):
    source, target = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"new")
    order = []
    real_replace = file_transfer.os.replace
    monkeypatch.setattr(file_transfer.os, "fsync", lambda fd: order.append("fsync"))
    monkeypatch.setattr(file_transfer.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b)))
    server.FileRename(str(source), str(target))
    assert order == ["fsync", "replace"]


@pytest.mark.parametrize("tool", ["FileHash", "FileRename"])
def test_the_new_tools_are_tier_3(tool):
    assert tool in TOOL_TIERS["tier3"]


def test_shell_carries_exact_structured_fields_beside_the_prefixed_text():
    result = _call("Shell", {"command": "printf ' out\\n\\n'; echo err >&2; exit 4"})
    assert result.structured_content == {"stdout": " out\n\n", "stderr": "err\n", "exit_code": 4}
    text = result.content[0].text
    assert text.startswith("[task:")
    assert text.endswith("] out\n\n\n[STDERR] err\n\n[Exit code: 4]")


def test_a_failing_shell_call_is_still_an_error_without_fields():
    result = asyncio.run(_failing_call())
    assert result.is_error
    assert "working directory not found" in result.content[0].text


async def _failing_call():
    async with Client(server.mcp) as client:
        return await client.call_tool("Shell", {"command": "true", "cwd": "/no/such/dir"}, raise_on_error=False)
