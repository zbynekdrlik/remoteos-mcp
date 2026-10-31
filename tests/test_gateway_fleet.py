"""The fleet server (#51 Phase 2) on the real stack: a real gateway and real FastMCP machines on
loopback sockets, a real MCP client as the consumer. Nothing internal is mocked."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import time
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.exceptions import ToolError
from fleet_harness import (
    PIXEL_PNG,
    LoopbackServer,
    RealMachine,
    closed_port_url,
    fresh_secret,
    write_private_json,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from remoteos.gateway import fleet as fleet_module
from remoteos.gateway.accesslog import AccessLog, read_access_log
from remoteos.gateway.app import MAX_BODY_BYTES, create_app
from remoteos.gateway.keystore import edit_keystore
from remoteos.gateway.registry import LiveConfig, MachineEntry, edit_registry, token_sha256

BOX = "linux-fleet-a"
OTHER_BOX = "linux-fleet-b"
FORBIDDEN_BOX = "linux-fleet-secret"
CONSUMER = "fleet-test@dev1"


class Stack:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.token = fresh_secret("consumer")
        self.key = fresh_secret("machinekey")
        self.registry = tmp / "registry.json"
        self.keystore = tmp / "machines.json"
        self.access_log = tmp / "access.jsonl"
        self.machine = RealMachine(self.key)
        self.machine.start()
        write_private_json(
            self.keystore,
            {
                BOX: {"url": self.machine.url, "key": self.key},
                FORBIDDEN_BOX: {"url": self.machine.url, "key": self.key},
            },
        )
        write_private_json(
            self.registry,
            {
                "exposed": [BOX, FORBIDDEN_BOX],
                "consumers": {
                    CONSUMER: {"sha256": [token_sha256(self.token)], "machines": [BOX], "added": "2026-10-31"}
                },
            },
        )
        self.app = create_app(LiveConfig(self.registry, self.keystore), AccessLog(self.access_log))
        self.gateway = LoopbackServer(self.app)
        self.gateway.start()

    @property
    def fleet_url(self) -> str:
        return f"{self.gateway.base_url}/fleet/mcp"

    def client(self, token: str | None = None) -> Client:
        return Client(self.fleet_url, auth=BearerAuth(token or self.token), mode="legacy")

    def add_machine(self, name: str, url: str, key: str) -> None:
        """What a manifest line + sync-configs + set-key leave behind, on the live files."""
        with edit_keystore(self.keystore) as machines:
            machines[name] = MachineEntry(url=url, key=key)
        with edit_registry(self.registry) as registry:
            registry.exposed.append(name)
            registry.consumers[CONSUMER].machines.append(name)

    def log(self) -> list[dict]:
        return read_access_log(self.access_log, tail=1000)

    def stop(self) -> None:
        self.gateway.stop(force=True)
        self.machine.stop(force=True)


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.stop()


def _budget_returns_to(stack: Stack, expected: int) -> None:
    """It is given back when the response has gone out, a moment after the client read it."""
    deadline = time.monotonic() + 10
    while stack.app.state.fleet.budget.used != expected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stack.app.state.fleet.budget.used == expected


async def _list(client: Client) -> dict[str, dict]:
    result = await client.call_tool("ListMachines")
    return {m["machine"]: m for m in result.structured_content["result"]}


# -- the three tools ----------------------------------------------------------------------


def test_tools_are_exactly_the_three_and_never_change(stack):
    async def go():
        async with stack.client() as client:
            return sorted(t.name for t in await client.list_tools())

    assert asyncio.run(go()) == ["Call", "ListMachines", "MachineTools"]


def test_list_machines_reports_os_version_and_reachability(stack):
    async def go():
        async with stack.client() as client:
            return await _list(client)

    assert asyncio.run(go()) == {
        BOX: {
            "machine": BOX,
            "reachable": True,
            "os": "Linux test",
            "hostname": "fakebox",
            "version": "9.9.9",
            "detail": None,
        }
    }


def test_machine_tools_gives_the_real_schemas(stack):
    async def go():
        async with stack.client() as client:
            return (await client.call_tool("MachineTools", {"machine": BOX})).structured_content["result"]

    tools = {t["name"]: t for t in asyncio.run(go())}
    assert set(tools) == {"Echo", "Snap", "Blob", "Boom"}
    assert tools["Echo"]["inputSchema"]["properties"]["text"]["type"] == "string"
    assert tools["Echo"]["description"] == "Echo the text."


def test_call_runs_the_tool_and_returns_its_text(stack):
    async def go():
        async with stack.client() as client:
            return await client.call_tool("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": "ahoj"}})

    result = asyncio.run(go())
    assert result.content[0].text == "echo: ahoj"
    assert stack.machine.received == [{"text": "ahoj"}]


def test_image_and_multi_content_results_pass_through_intact(stack):
    async def go():
        async with stack.client() as client:
            return await client.call_tool("Call", {"machine": BOX, "tool": "Snap"})

    content = asyncio.run(go()).content
    assert [block.type for block in content] == ["text", "image"]
    assert content[0].text == "a caption"
    assert content[1].mime_type == "image/png"
    assert base64.b64decode(content[1].data) == PIXEL_PNG


def test_a_tool_error_stays_an_mcp_error(stack):
    async def go():
        async with stack.client() as client:
            return await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Boom"})

    result = asyncio.run(go())
    assert result.is_error is True
    assert "box says no" in result.content[0].text


def test_a_tool_error_raises_in_a_client_that_raises(stack):
    async def go():
        async with stack.client() as client:
            with pytest.raises(ToolError, match="box says no"):
                await client.call_tool("Call", {"machine": BOX, "tool": "Boom"})

    asyncio.run(go())


def test_an_unknown_tool_on_the_machine_is_an_error_too(stack):
    async def go():
        async with stack.client() as client:
            return await client.call_tool_mcp("Call", {"machine": BOX, "tool": "NoSuchTool"})

    result = asyncio.run(go())
    assert result.is_error is True
    assert "NoSuchTool" in result.content[0].text


# -- same token, allow-list, kill switch ----------------------------------------------------


def test_a_machine_outside_the_allow_list_is_refused_and_absent(stack):
    async def go():
        async with stack.client() as client:
            listed = await _list(client)
            arguments = {"machine": FORBIDDEN_BOX, "tool": "Echo", "arguments": {"text": "x"}}
            refused = await client.call_tool_mcp("Call", arguments)
            tools = await client.call_tool_mcp("MachineTools", {"machine": FORBIDDEN_BOX})
            return listed, refused, tools

    listed, refused, tools = asyncio.run(go())
    assert FORBIDDEN_BOX not in listed
    assert refused.is_error and "not allowed" in refused.content[0].text
    assert tools.is_error and "not allowed" in tools.content[0].text
    assert stack.machine.received == []  # it never reached the box
    denied = [r for r in stack.log() if r["outcome"] == "denied_not_allowed"]
    assert [(r["machine"], r["consumer"], r["tool"], r["status"]) for r in denied] == [
        (FORBIDDEN_BOX, CONSUMER, "Call", 403),
        (FORBIDDEN_BOX, CONSUMER, "MachineTools", 403),
    ]


def test_an_unknown_machine_answers_exactly_like_a_forbidden_one(stack):
    """A consumer cannot probe which other machines exist."""

    async def go():
        async with stack.client() as client:
            a = await client.call_tool_mcp("Call", {"machine": "win-does-not-exist", "tool": "Echo"})
            b = await client.call_tool_mcp("Call", {"machine": FORBIDDEN_BOX, "tool": "Echo"})
            return a.content[0].text, b.content[0].text

    unknown, forbidden = asyncio.run(go())
    assert unknown.replace("win-does-not-exist", "X") == forbidden.replace(FORBIDDEN_BOX, "X")


def test_a_bad_machine_name_is_refused_and_never_logged_raw(stack):
    bad = "../evil\nname"

    async def go():
        async with stack.client() as client:
            return await client.call_tool_mcp("Call", {"machine": bad, "tool": "Echo"})

    assert asyncio.run(go()).is_error
    assert "evil" not in stack.access_log.read_text()
    assert [r["machine"] for r in stack.log() if r["outcome"] == "denied_not_allowed"] == ["<invalid>"]


def test_the_kill_switch_stops_a_running_session(stack):
    async def go():
        async with stack.client() as client:
            first = await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": "1"}})
            with edit_registry(stack.registry) as registry:
                registry.exposed.remove(BOX)
            second = await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": "2"}})
            return first, second, await _list(client)

    first, second, listed = asyncio.run(go())
    assert not first.is_error
    assert second.is_error and "not available" in second.content[0].text
    assert listed == {}
    assert stack.machine.received == [{"text": "1"}]


def test_the_endpoint_needs_a_token_like_every_other(stack):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"accept": "application/json, text/event-stream"}

    missing = httpx2.post(stack.fleet_url, json=body, headers=headers)
    wrong = httpx2.post(stack.fleet_url, json=body, headers={**headers, "Authorization": "Bearer wrong"})
    assert (missing.status_code, wrong.status_code) == (401, 401)
    assert [r["outcome"] for r in stack.log()] == ["denied_missing_token", "denied_unknown_token"]


def test_a_body_over_the_cap_gets_413_before_anything_runs(stack):
    response = httpx2.post(
        stack.fleet_url,
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Authorization": f"Bearer {stack.token}", "content-type": "application/json"},
        timeout=30,
    )
    assert response.status_code == 413
    assert stack.log()[-1]["outcome"] == "payload_too_large"


def test_a_revoked_consumer_loses_a_running_session(stack):
    async def go():
        async with stack.client() as client:
            await _list(client)
            with edit_registry(stack.registry) as registry:
                registry.revoke_consumer(CONSUMER)
            with pytest.raises(Exception):
                await client.call_tool("ListMachines")

    asyncio.run(go())
    assert stack.log()[-1]["outcome"] == "denied_unknown_token"


# -- the machine and manifest are live -------------------------------------------------------


def test_a_machine_added_while_the_session_runs_works_at_once(stack):
    new_key = fresh_secret("machinekey")
    with RealMachine(new_key, os_name="Windows test", version="1.2.3") as new_box:

        async def go():
            async with stack.client() as client:  # ONE session across the change, no reconnect
                before = await _list(client)
                stack.add_machine(OTHER_BOX, new_box.url, new_key)
                after = await _list(client)
                arguments = {"machine": OTHER_BOX, "tool": "Echo", "arguments": {"text": "nový"}}
                return before, after, await client.call_tool("Call", arguments)

        before, after, called = asyncio.run(go())
    assert OTHER_BOX not in before
    assert after[OTHER_BOX]["os"] == "Windows test"
    assert after[OTHER_BOX]["version"] == "1.2.3"
    assert called.content[0].text == "echo: nový"
    assert new_box.received == [{"text": "nový"}]


def test_a_rotated_key_reaches_a_running_session(stack):
    rotated = fresh_secret("machinekey")
    stack.machine.key = rotated

    async def go():
        async with stack.client() as client:
            stale = await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": "1"}})
            with edit_keystore(stack.keystore) as machines:
                machines[BOX] = MachineEntry(url=stack.machine.url, key=rotated)
            fresh = await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": "2"}})
            return stale, fresh

    stale, fresh = asyncio.run(go())
    assert stale.is_error and "rejected the gateway's key" in stale.content[0].text
    assert not fresh.is_error


# -- a box that is down -----------------------------------------------------------------------


def test_a_box_that_is_down_is_a_clear_error_and_does_not_hang_the_list(stack):
    stack.add_machine(OTHER_BOX, closed_port_url(), fresh_secret("machinekey"))

    async def go():
        async with stack.client() as client:
            started = time.monotonic()
            listed = await _list(client)
            called = await client.call_tool_mcp("Call", {"machine": OTHER_BOX, "tool": "Echo"})
            return listed, called, time.monotonic() - started

    listed, called, elapsed = asyncio.run(go())
    assert listed[OTHER_BOX]["reachable"] is False
    assert listed[OTHER_BOX]["version"] is None
    assert listed[BOX]["reachable"] is True
    assert called.is_error
    assert OTHER_BOX in called.content[0].text and "unreachable" in called.content[0].text
    assert elapsed < 5


def test_a_box_that_accepts_and_says_nothing_cannot_hold_the_list(stack, monkeypatch):
    """A black hole: the probes are parallel and each is cut off, so the wait is one bound, not N."""
    monkeypatch.setattr(fleet_module, "PROBE_BOUND_SECONDS", 0.3)

    async def silent(request):
        await asyncio.Event().wait()

    with LoopbackServer(Starlette(routes=[Route("/health", silent)])) as hole:
        for index in range(5):
            stack.add_machine(f"linux-hole-{index}", f"{hole.base_url}/mcp", fresh_secret("machinekey"))

        async def go():
            async with stack.client() as client:
                started = time.monotonic()
                listed = await _list(client)
                return listed, time.monotonic() - started

        listed, elapsed = asyncio.run(go())
    assert elapsed < 1.2  # five machines at 0.3 s each would be 1.5 s in series
    assert [listed[f"linux-hole-{i}"]["reachable"] for i in range(5)] == [False] * 5
    assert listed[BOX]["reachable"] is True


def test_a_server_without_health_is_listed_as_up_with_no_version(stack):
    key = fresh_secret("machinekey")
    with RealMachine(key, health=False) as old_box:
        stack.add_machine(OTHER_BOX, old_box.url, key)

        async def go():
            async with stack.client() as client:
                return await _list(client)

        listed = asyncio.run(go())
    assert listed[OTHER_BOX]["reachable"] is True
    assert listed[OTHER_BOX]["version"] is None


def test_a_box_that_dies_during_a_call_is_an_error_not_a_hang(stack):
    """The answer has begun (200 + headers), then the connection is gone."""
    stack.machine.die_at_tool_answer = True

    async def go():
        async with stack.client() as client:
            arguments = {"machine": BOX, "tool": "Echo", "arguments": {"text": "x"}}
            return await asyncio.wait_for(client.call_tool_mcp("Call", arguments), 20)

    result = asyncio.run(go())
    assert stack.machine.received == [{"text": "x"}]  # the tool ran: it died on the answer
    assert result.is_error
    assert BOX in result.content[0].text


def test_a_reply_over_one_mebibyte_arrives_whole(stack):
    """The SDK's own limit on one event is 1 MiB: a FileDownload of 780 KB would pass it."""
    size = 3 * 1024 * 1024

    async def go():
        async with stack.client() as client:
            return await client.call_tool("Call", {"machine": BOX, "tool": "Blob", "arguments": {"size": size}})

    assert asyncio.run(go()).content[0].text == "x" * size
    _budget_returns_to(stack, 0)


def test_a_reply_over_the_cap_is_a_clear_error_not_an_out_of_memory(stack):
    size = fleet_module.MAX_REPLY_BYTES + 1

    async def go():
        async with stack.client() as client:
            big = await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Blob", "arguments": {"size": size}})
            small = await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Blob", "arguments": {"size": 10}})
            return big, small

    big, small = asyncio.run(go())
    assert big.is_error and "too large" in big.content[0].text
    assert not small.is_error
    _budget_returns_to(stack, 0)  # the refused reply gave its bytes back


def test_replies_together_cannot_pass_the_budget(stack):
    held = fleet_module.MAX_BUFFERED_BYTES - 1024  # what other calls in flight hold right now
    stack.app.state.fleet.budget.used = held

    async def go():
        async with stack.client() as client:
            return await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Blob", "arguments": {"size": 4096}})

    result = asyncio.run(go())
    assert result.is_error and "try again" in result.content[0].text
    _budget_returns_to(stack, held)


def test_the_budget_is_held_until_the_consumer_has_the_reply(stack):
    """The parsed and re-serialised copies outlive the machine's reply: they are what costs memory."""
    fleet = stack.app.state.fleet
    seen: list[int] = []
    real_send = fleet.http

    async def watch_the_outer_response(scope, receive, send):
        async def watching(message):
            if message["type"] == "http.response.body" and b"xxxx" in message.get("body", b""):
                seen.append(fleet.budget.used)
            await send(message)

        await real_send(scope, receive, watching)

    fleet.http = watch_the_outer_response

    async def go():
        async with stack.client() as client:
            arguments = {"machine": BOX, "tool": "Blob", "arguments": {"size": 2_000_000}}
            await client.call_tool("Call", arguments)

    asyncio.run(go())
    assert seen and min(seen) >= 4_000_000  # the text is in the reply twice
    _budget_returns_to(stack, 0)


def test_a_consumer_that_stops_reading_gives_the_budget_back(stack, monkeypatch):
    monkeypatch.setattr(fleet_module, "SEND_TIMEOUT_SECONDS", 0.5)
    arguments = {"machine": BOX, "tool": "Blob", "arguments": {"size": 8_000_000}}
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "Call", "arguments": arguments}}
    ).encode()
    head = (
        f"POST /fleet/mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {stack.token}\r\n"
        "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
        f"Mcp-Protocol-Version: 2025-06-18\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", stack.gateway.port)) as stalled:
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stalled.sendall(head + body)  # and never reads the answer
        deadline = time.monotonic() + 5
        while stack.app.state.fleet.budget.used == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stack.app.state.fleet.budget.used > 0
        _budget_returns_to(stack, 0)


def test_a_call_never_lists_the_tools_after_the_tool_ran(stack):
    """A tools/list after the call could turn a finished call into an error, and a retry."""

    async def go():
        async with stack.client() as client:
            await client.call_tool("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": "x"}})

    asyncio.run(go())
    methods = [r["rpc_method"] for r in stack.log() if r["machine"] == BOX]
    assert "tools/call" in methods and "tools/list" not in methods


def test_a_machine_silent_after_initialize_cannot_hold_machine_tools(stack, monkeypatch):
    monkeypatch.setattr(fleet_module, "_TOOLS_TIMEOUT_SECONDS", 0.5)
    key = fresh_secret("machinekey")

    async def initialize_then_nothing(request):
        message = await request.json()
        if message.get("method") == "tools/list":
            await asyncio.Event().wait()
        if "id" not in message:
            return Response(status_code=202)
        result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "s", "version": "1"}}
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})

    with LoopbackServer(Starlette(routes=[Route("/mcp", initialize_then_nothing, methods=["POST"])])) as box:
        stack.add_machine(OTHER_BOX, f"{box.base_url}/mcp", key)

        async def go():
            async with stack.client() as client:
                started = time.monotonic()
                result = await asyncio.wait_for(client.call_tool_mcp("MachineTools", {"machine": OTHER_BOX}), 20)
                return result, time.monotonic() - started

        result, elapsed = asyncio.run(go())
    assert result.is_error and elapsed < 10


def test_losing_a_machine_during_the_call_is_not_called_a_stale_key(stack):
    """A deny that lands between the check and the machine: the gateway's refusal, in its words."""

    fleet = stack.app.state.fleet
    machine_app = fleet.machine_app

    async def deny_then_forward(scope, receive, send):
        with edit_registry(stack.registry) as registry:
            registry.consumers[CONSUMER].machines = []
        await machine_app(scope, receive, send)

    fleet.machine_app = deny_then_forward  # runs after _admit, before Gateway.handle

    async def go():
        async with stack.client() as client:
            return await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": "x"}})

    result = asyncio.run(go())
    assert result.is_error
    assert "not allowed" in result.content[0].text
    assert "stale" not in result.content[0].text
    assert [r["tool"] for r in stack.log() if r["outcome"] == "denied_not_allowed"] == [None]  # handle refused it
    assert stack.machine.received == []


def test_a_refused_request_is_not_logged_as_ok(stack):
    response = httpx2.post(
        stack.fleet_url,
        content=b"not json",
        headers={
            "Authorization": f"Bearer {stack.token}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 400
    deadline = time.monotonic() + 5  # the line is written once the response has gone out
    while not stack.log() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stack.log()[-1]["outcome"] == "fleet_refused"


def test_a_quieter_log_level_stays_quiet():
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.WARNING)
    try:
        fleet_module.keep_arguments_out_of_the_logs()
        assert logging.getLogger("mcp").level == logging.WARNING
    finally:
        root.setLevel(before)


# -- nothing secret reaches a log ------------------------------------------------------------


def test_no_token_key_or_argument_reaches_any_log(stack, caplog, capfd):
    secret_argument = fresh_secret("argument")
    caplog.set_level(logging.DEBUG)
    fleet_module.keep_arguments_out_of_the_logs()  # as `serve --log-level debug` does, in this order

    async def go():
        async with stack.client() as client:
            await client.call_tool("Call", {"machine": BOX, "tool": "Echo", "arguments": {"text": secret_argument}})
            await client.call_tool_mcp("Call", {"machine": BOX, "tool": "Boom", "arguments": {"bad": secret_argument}})
            bad_type = {"machine": BOX, "tool": "Echo", "arguments": {"text": {"x": secret_argument}}}
            await client.call_tool_mcp("Call", bad_type)
            await client.call_tool_mcp("Call", {"machine": 7, "tool": secret_argument})

    asyncio.run(go())
    assert stack.machine.received == [{"text": secret_argument}]
    records = stack.access_log.read_text()
    captured = capfd.readouterr()
    for haystack in (records, caplog.text, captured.out, captured.err):
        assert secret_argument not in haystack
        assert stack.token not in haystack
        assert stack.key not in haystack
    # the audit trail is still there: the outer request and the machine's own line
    tools = {(r["machine"], r["tool"]) for r in map(json.loads, records.splitlines())}
    assert (BOX, "Echo") in tools
    assert ("fleet", "Call") in tools
    assert all(r["consumer"] == CONSUMER for r in stack.log() if r["outcome"] == "ok")
