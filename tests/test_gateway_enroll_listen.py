"""The permanent enrollment endpoint (#101): ``enroll-listen`` and its queue.

Every request goes over real HTTP to the real Starlette app under uvicorn on loopback
(``LoopbackServer``, in-process so caplog sees the log lines); one test starts the real
``remoteos-gateway enroll-listen`` command as a child process. The queue is real files.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from fleet_harness import LoopbackServer, free_port, fresh_secret
from starlette.requests import Request

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway import enroll_listen, enroll_queue
from remoteos.gateway.enroll_listen import LOG_LIMIT, REGISTRATION_LIMIT, RateLimiter, client_address, create_app
from remoteos.gateway.enroll_queue import MAX_PENDING, PENDING_TTL_S, EnrollQueue

UA = {"user-agent": "remoteos-enroll"}


def body(key: str, **overrides: object) -> dict:
    return {"hostname": "predaj3", "ips": ["10.77.8.31", "8.8.8.8", "127.0.0.1"], "port": 8092,
            "version": "0.7.0.dev61", "os": "windows", "account": "pokladna", "auth_key": key, **overrides}


class Endpoint:
    def __init__(self, root: Path, limiter: RateLimiter | None = None, log_limiter: RateLimiter | None = None) -> None:
        self.queue = EnrollQueue(root)
        self.server = LoopbackServer(create_app(self.queue, limiter=limiter, log_limiter=log_limiter))
        self.client = httpx.Client(base_url=self.server.base_url, headers=UA, timeout=10, trust_env=False)

    def post(self, payload: object, *, ip: str = "203.0.113.7", path: str = "/enroll/", **kwargs) -> httpx.Response:
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"content-type": "application/json", "cf-connecting-ip": ip, **kwargs.pop("headers", {})}
        return self.client.post(path, content=content, headers=headers, **kwargs)


@pytest.fixture
def endpoint(tmp_path):
    world = Endpoint(tmp_path / "enroll")
    with world.server:
        yield world
    world.client.close()


def _invoke(*args: str):
    return CliRunner().invoke(gateway_cli.main, list(args))


def test_a_registration_is_queued_0600_and_answered_ok(endpoint, caplog):
    caplog.set_level(logging.INFO)
    key = fresh_secret("Box")
    reply = endpoint.post(body(key))
    assert reply.status_code == 200 and reply.text == "ok"  # the installers count only this body
    [entry] = endpoint.queue.pending()
    assert entry.registration.hostname == "predaj3" and entry.registration.key == key
    assert entry.registration.addresses == ("10.77.8.31",)  # a public and a loopback address never kept
    assert entry.source == "203.0.113.7"
    path = endpoint.queue.pending_dir / f"{entry.id}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(endpoint.queue.pending_dir.stat().st_mode) == 0o700
    assert "predaj3" in caplog.text and key not in caplog.text


def test_the_trailing_slash_less_path_works_too_and_a_retry_is_one_entry(endpoint):
    key = fresh_secret("Box")
    assert endpoint.post(body(key), path="/enroll").status_code == 200
    assert endpoint.post(body(key)).status_code == 200
    assert len(endpoint.queue.pending()) == 1
    assert endpoint.post(body(fresh_secret("Other"))).status_code == 200  # same name, another key: its own entry
    assert len(endpoint.queue.pending()) == 2


@pytest.mark.parametrize("payload, field", [
    (b"not json", "not JSON"),
    ({"hostname": "x"}, "valid version"),
    ("auth_key", "valid auth_key"),
    ("port", "valid port"),
    ("hostname", "valid hostname"),
])
def test_an_invalid_registration_is_400_names_the_field_and_queues_nothing(endpoint, caplog, payload, field):
    key = fresh_secret("Leak")
    if payload == "auth_key":
        payload = body(key + "!")  # not 1-128 letters/digits
    elif payload == "port":
        payload = body(key, port=70000)
    elif payload == "hostname":
        payload = body(key, hostname="evil host; rm -rf /")
    reply = endpoint.post(payload)
    assert reply.status_code == 400 and field in reply.text
    assert key not in reply.text and key not in caplog.text
    assert endpoint.queue.pending() == []


def test_a_registration_over_16_kib_is_413_declared_or_not(endpoint):
    big = body(fresh_secret("Box"), padding="x" * REGISTRATION_LIMIT)
    assert endpoint.post(big).status_code == 413

    def chunks():  # no Content-Length: the cap must hold while the body streams
        yield json.dumps(big).encode()

    reply = endpoint.client.post("/enroll/", content=chunks(), headers={"cf-connecting-ip": "203.0.113.8"})
    assert reply.status_code == 413
    assert endpoint.queue.pending() == []


def test_the_rate_limit_is_per_address_and_overall(tmp_path):
    world = Endpoint(tmp_path / "enroll", RateLimiter(per_ip=3, overall=5))
    with world.server:
        assert [world.post(body(fresh_secret("A")), ip="198.51.100.1").status_code for _ in range(4)] == [
            200, 200, 200, 429]
        assert world.post(body(fresh_secret("B")), ip="198.51.100.2").status_code == 200
        assert world.post(body(fresh_secret("C")), ip="198.51.100.3").status_code == 200
        assert world.post(body(fresh_secret("D")), ip="198.51.100.4").status_code == 429  # five in the window
    world.client.close()


def test_the_window_slides():
    now = [0.0]
    limiter = RateLimiter(per_ip=1, overall=10, window_s=60, clock=lambda: now[0])
    assert limiter.allow("a") and not limiter.allow("a")
    now[0] = 61
    assert limiter.allow("a")


def test_cf_connecting_ip_is_trusted_only_from_loopback():
    def request(peer: str, header: str | None) -> Request:
        headers = [(b"cf-connecting-ip", header.encode())] if header else []
        return Request({"type": "http", "client": (peer, 5555), "headers": headers})

    assert client_address(request("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert client_address(request("192.0.2.1", "203.0.113.9")) == "192.0.2.1"
    assert client_address(request("127.0.0.1", "not an ip")) == "127.0.0.1"


def test_a_full_queue_answers_503(endpoint, monkeypatch):
    monkeypatch.setattr(enroll_queue, "MAX_PENDING", 2)
    for index in range(2):
        reply = endpoint.post(body(fresh_secret("Box"), hostname=f"pc{index}"), ip=f"198.51.100.{index}")
        assert reply.status_code == 200
    assert endpoint.post(body(fresh_secret("Box"), hostname="pc9"), ip="198.51.100.9").status_code == 503
    assert MAX_PENDING >= 10  # the real cap leaves room for a site's batch


def test_only_the_routes_exist(endpoint):
    assert endpoint.client.get("/healthz").status_code == 204
    assert endpoint.client.get("/enroll/").status_code == 405
    assert endpoint.client.post("/anything", content=b"{}").status_code == 404
    assert endpoint.client.post("/enroll/x", content=b"{}").status_code == 404


def test_an_install_log_is_kept_with_validated_headers_and_listed(endpoint, caplog):
    caplog.set_level(logging.INFO)
    text = "  [X] pip failed: Žltý kôň\n" * 10
    reply = endpoint.client.post("/enroll/log", content=text.encode(), headers={
        "x-remoteos-host": "DSG", "x-remoteos-os": "windows", "x-remoteos-exit": "1",
        "cf-connecting-ip": "198.51.100.5"})
    assert reply.status_code == 200 and reply.text == "ok"
    endpoint.client.post("/enroll/log", content=b"boom", headers={
        "x-remoteos-host": "bad host;x", "x-remoteos-os": "plan9", "x-remoteos-exit": "1; rm"})
    first, second = endpoint.queue.logs()
    assert (first.hostname, first.os, first.exit_code, first.source) == ("DSG", "windows", "1", "198.51.100.5")
    assert (second.hostname, second.os, second.exit_code) == ("unknown", "unknown", "")
    assert endpoint.queue.read_log(first.id) == text
    assert stat.S_IMODE((endpoint.queue.logs_dir / f"{first.id}.json").stat().st_mode) == 0o600
    listed = _invoke("enroll-log", "--enroll-dir", str(endpoint.queue.root))
    assert listed.exit_code == 0 and first.id in listed.output and "DSG" in listed.output
    shown = _invoke("enroll-log", first.id, "--enroll-dir", str(endpoint.queue.root))
    assert shown.output == text
    assert "pip failed" not in caplog.text  # the log body goes to its file only


def test_an_install_log_over_256_kib_is_413(endpoint):
    reply = endpoint.client.post("/enroll/log", content=b"x" * (LOG_LIMIT + 1))
    assert reply.status_code == 413 and endpoint.queue.logs() == []


def test_only_the_newest_logs_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(enroll_queue, "MAX_LOGS", 3)
    now = [1000.0]
    queue = EnrollQueue(tmp_path, clock=lambda: now[0])
    ids = []
    for index in range(5):
        now[0] += 1
        ids.append(queue.add_log(f"log {index}", hostname="pc", os_name="linux", exit_code="1", source="::1"))
    assert [entry.id for entry in queue.logs()] == ids[2:]


def test_a_registration_expires_after_24_hours(tmp_path):
    now = [1000.0]
    queue = EnrollQueue(tmp_path, clock=lambda: now[0])
    entry, _ = queue.add(enroll_queue.parse_registration(json.dumps(body(fresh_secret("K"))).encode()), "::1")
    now[0] += PENDING_TTL_S + 1
    assert queue.pending() == []
    assert not (queue.pending_dir / f"{entry.id}.json").exists()  # the key is gone with it


def test_a_tampered_queue_file_is_skipped_never_trusted(tmp_path, caplog):
    queue = EnrollQueue(tmp_path)
    entry, _ = queue.add(enroll_queue.parse_registration(json.dumps(body(fresh_secret("K"))).encode()), "::1")
    path = queue.pending_dir / f"{entry.id}.json"
    data = json.loads(path.read_text())
    data["registration"]["port"] = "8092; evil"
    path.write_text(json.dumps(data))
    assert queue.pending() == []
    assert "skipped" in caplog.text
    assert not queue.remove("../x") and not queue.remove("ZZZZZZZZ")


def test_enroll_list_never_shows_a_key(endpoint):
    key = fresh_secret("Box")
    endpoint.post(body(key))
    result = _invoke("enroll-list", "--enroll-dir", str(endpoint.queue.root))
    assert result.exit_code == 0
    assert "predaj3" in result.output and "10.77.8.31" in result.output and "203.0.113.7" in result.output
    assert key not in result.output


def test_enroll_reject_deletes_the_registration(endpoint):
    endpoint.post(body(fresh_secret("Box")))
    [entry] = endpoint.queue.pending()
    result = _invoke("enroll-reject", entry.id, "--enroll-dir", str(endpoint.queue.root))
    assert result.exit_code == 0 and endpoint.queue.pending() == []
    assert _invoke("enroll-reject", entry.id, "--enroll-dir", str(endpoint.queue.root)).exit_code == 1


def test_the_real_command_binds_loopback_only_and_serves(tmp_path):
    refused = _invoke("enroll-listen", "--host", "0.0.0.0", "--enroll-dir", str(tmp_path))
    assert refused.exit_code == 2 and "loopback only" in refused.output
    port = free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    child = subprocess.Popen(
        [sys.executable, "-m", "remoteos.gateway.cli", "enroll-listen", "--port", str(port), "--enroll-dir",
         str(tmp_path / "q")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1, trust_env=False).status_code == 204:
                    break
            except httpx.HTTPError:
                if time.monotonic() > deadline or child.poll() is not None:
                    raise
            time.sleep(0.1)
        key = fresh_secret("Real")
        reply = httpx.post(f"http://127.0.0.1:{port}/enroll/", json=body(key), timeout=5, trust_env=False,
                           headers={**UA, "x-forwarded-for": "192.0.2.50"})
        assert reply.text == "ok"
        [entry] = EnrollQueue(tmp_path / "q").pending()
        assert entry.source == "127.0.0.1"  # X-Forwarded-For is never trusted (proxy_headers off)
    finally:
        child.terminate()
        output = child.communicate(timeout=15)[0].decode()
    assert "registration" in output and key not in output


def test_the_listener_has_no_debug_level_that_would_print_bodies():
    result = _invoke("enroll-listen", "--log-level", "debug")
    assert result.exit_code == 2
    assert enroll_listen.DEFAULT_LISTEN_HOST == "127.0.0.1"


def test_one_address_can_hold_only_a_few_registrations(endpoint, monkeypatch):
    monkeypatch.setattr(enroll_queue, "MAX_PER_SOURCE", 2)
    codes = [endpoint.post(body(fresh_secret("Box"), hostname=f"pc{i}"), ip="198.51.100.20").status_code
             for i in range(3)]
    assert codes == [200, 200, 503]
    assert endpoint.post(body(fresh_secret("Box"), hostname="pc9"), ip="198.51.100.21").status_code == 200


def test_an_ipv6_poster_counts_per_64(tmp_path):
    world = Endpoint(tmp_path / "enroll", RateLimiter(per_ip=2, overall=100))
    with world.server:
        codes = [world.post(body(fresh_secret("A"), hostname=f"pc{i}"), ip=f"2001:db8:1:2::{i + 1}").status_code
                 for i in range(3)]
        assert codes == [200, 200, 429]  # three addresses of ONE /64 = one poster
        assert world.post(body(fresh_secret("B")), ip="2001:db8:1:3::1").status_code == 200
    world.client.close()
    assert enroll_queue.source_group("2001:db8:1:2::99") == "2001:db8:1:2::/64"
    assert enroll_queue.source_group("::ffff:192.0.2.1") == "192.0.2.1"


def test_a_log_flood_never_costs_a_registration_its_budget(tmp_path):
    world = Endpoint(tmp_path / "enroll", RateLimiter(per_ip=2, overall=2), RateLimiter(per_ip=1, overall=1))
    with world.server:
        assert world.client.post("/enroll/log", content=b"x").status_code == 200
        assert world.client.post("/enroll/log", content=b"x").status_code == 429
        assert world.post(body(fresh_secret("A"))).status_code == 200
    world.client.close()


def test_enroll_log_never_prints_a_terminal_escape(tmp_path):
    queue = EnrollQueue(tmp_path)
    log_id = queue.add_log("ok line\n\x1b]0;owned\x07\x1b[2Jtail\tx\n", hostname="pc", os_name="linux",
                           exit_code="1", source="::1")
    shown = _invoke("enroll-log", log_id, "--enroll-dir", str(tmp_path))
    assert "\x1b" not in shown.output and "\x07" not in shown.output
    assert shown.output == "ok line\n?]0;owned??[2Jtail\tx\n"


def test_reject_by_source_or_all_and_only_one_selector(endpoint):
    for index, ip in enumerate(("198.51.100.30", "198.51.100.30", "198.51.100.31")):
        endpoint.post(body(fresh_secret("Box"), hostname=f"pc{index}"), ip=ip)
    root = str(endpoint.queue.root)
    assert _invoke("enroll-reject", "--enroll-dir", root).exit_code == 2
    assert _invoke("enroll-reject", "--all", "--source", "198.51.100.30", "--enroll-dir", root).exit_code == 2
    result = _invoke("enroll-reject", "--source", "198.51.100.30", "--enroll-dir", root)
    assert "2 registration(s)" in result.output
    assert [entry.source for entry in endpoint.queue.pending()] == ["198.51.100.31"]
    assert "1 registration(s)" in _invoke("enroll-reject", "--all", "--enroll-dir", root).output
    assert endpoint.queue.pending() == []


def test_an_unreadable_queue_raises_and_check_keeps_the_last_list(tmp_path):
    from remoteos.gateway.cli_fleet import _enroll_pending

    queue = EnrollQueue(tmp_path)
    queue.add(enroll_queue.parse_registration(json.dumps(body(fresh_secret("K"))).encode()), "::1")
    queue.pending_dir.chmod(0)
    try:
        with pytest.raises(OSError):
            queue.pending()
        assert _enroll_pending(tmp_path) is None  # None = keep the last list, never "empty"
    finally:
        queue.pending_dir.chmod(0o700)


def test_reject_by_source_takes_the_whole_ipv6_64(endpoint):
    for index in range(2):
        endpoint.post(body(fresh_secret("Box"), hostname=f"pc{index}"), ip=f"2001:db8:5:6::{index + 1}")
    endpoint.post(body(fresh_secret("Box"), hostname="kept"), ip="2001:db8:5:7::1")
    result = _invoke("enroll-reject", "--source", "2001:db8:5:6::abcd", "--enroll-dir", str(endpoint.queue.root))
    assert "2 registration(s)" in result.output
    assert [entry.registration.hostname for entry in endpoint.queue.pending()] == ["kept"]


def test_an_unreadable_queue_is_a_clean_error_not_a_traceback(tmp_path):
    queue = EnrollQueue(tmp_path)
    queue.add(enroll_queue.parse_registration(json.dumps(body(fresh_secret("K"))).encode()), "::1")
    queue.pending_dir.chmod(0)
    try:
        for args in (["enroll-list"], ["enroll-reject", "--all"]):
            result = _invoke(*args, "--enroll-dir", str(tmp_path))
            assert result.exit_code == 1 and "cannot be read or written (PermissionError" in result.output
            assert not isinstance(result.exception, PermissionError)
    finally:
        queue.pending_dir.chmod(0o700)
