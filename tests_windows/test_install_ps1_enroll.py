"""install.ps1's enrollment parts in real PowerShell (#26): the functions are parsed out of the real
file, as in test_install_ps1_key.py. OpenSSH and the Python installer change the runner, so the
service half and the installers themselves are the supervisor's live run.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_install_ps1_key import _ps, _q

SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake dev1"


@pytest.mark.parametrize(("url", "ok"), [
    ("https://rough-sea.trycloudflare.com/EnrollTok_en-1/", True),
    ("http://rough-sea.trycloudflare.com/tok/", False),
    ("https://x.com/tok", False),
    ("https://x.com/a/b/", False),
    ("https://x.com/tok/\n", False),
    ("", False),
])
def test_enroll_url(tmp_path, url, ok):
    assert _ps(tmp_path, f"@{{ ok = Test-RemoteOSEnrollUrl {_q(url)} }} | ConvertTo-Json -Compress")["ok"] is ok


@pytest.mark.parametrize(("key", "ok"), [
    (SSH_KEY, True), ("ssh-rsa AAAA", True), ('command="x" ssh-rsa AAAA', False), (SSH_KEY + "\nssh-rsa B", False),
    ("", False),
])
def test_ssh_public_key(tmp_path, key, ok):
    assert _ps(tmp_path, f"@{{ ok = Test-RemoteOSSshPublicKey {_q(key)} }} | ConvertTo-Json -Compress")["ok"] is ok


def test_authorized_key_is_added_once_without_a_bom_and_keeps_the_others(tmp_path):
    path = tmp_path / ".ssh" / "authorized_keys"
    path.parent.mkdir()
    path.write_bytes(b"ssh-rsa AAAAother me")
    _ps(tmp_path, f"""
Add-RemoteOSAuthorizedKey -Path {_q(path)} -Key {_q(SSH_KEY)}
Add-RemoteOSAuthorizedKey -Path {_q(path)} -Key {_q(SSH_KEY)}
@{{ ok = $true }} | ConvertTo-Json -Compress
""")
    assert path.read_bytes() == f"ssh-rsa AAAAother me\n{SSH_KEY}\n".encode()


def test_authorized_key_makes_its_folder(tmp_path):
    path = tmp_path / "new" / "authorized_keys"
    _ps(tmp_path, f"""
Add-RemoteOSAuthorizedKey -Path {_q(path)} -Key {_q(SSH_KEY)}
@{{ ok = $true }} | ConvertTo-Json -Compress
""")
    assert path.read_bytes() == f"{SSH_KEY}\n".encode()


def test_an_administrator_uses_the_shared_file_a_standard_user_his_own(tmp_path):
    result = _ps(tmp_path, f"""
$where = @{{ DataRoot = {_q(tmp_path)}; UserProfile = {_q(tmp_path)} }}
@{{ admin = Get-RemoteOSAuthorizedKeysPath -Admin $true @where
   user = Get-RemoteOSAuthorizedKeysPath -Admin $false @where }} | ConvertTo-Json -Compress
""")
    assert result["admin"].replace("/", "\\").endswith(r"ssh\administrators_authorized_keys")
    assert result["user"].replace("/", "\\").endswith(r".ssh\authorized_keys")


def test_machine_python_needs_both_files(tmp_path):
    (tmp_path / "python.exe").write_text("")
    script = f"@{{ ok = Test-RemoteOSMachinePython {_q(tmp_path)} }} | ConvertTo-Json -Compress"
    assert _ps(tmp_path, script)["ok"] is False
    (tmp_path / "python312.dll").write_text("")
    assert _ps(tmp_path, script)["ok"] is True


def test_body_keeps_one_address_a_list(tmp_path):
    body = _ps(tmp_path, """
Get-RemoteOSEnrollmentBody -HostName 'pc' -Addresses @('10.0.0.5') -ServerPort 8092 -Version '1.0' -AuthKey 'abc'
""")
    assert body == {"hostname": "pc", "ips": ["10.0.0.5"], "port": 8092, "version": "1.0", "os": "windows",
                    "auth_key": "abc"}


class _Receiver:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses, self.seen = statuses, []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                return

            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers["content-length"])
                receiver.seen.append((self.path, self.headers["user-agent"], self.rfile.read(length)))
                self.send_response(receiver.statuses.pop(0))
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/tok/"


def test_send_posts_the_exact_bytes_with_a_named_agent_and_retries(tmp_path):
    receiver = _Receiver([500, 200])
    result = _ps(tmp_path, f"""
$e = Send-RemoteOSEnrollment -Url {_q(receiver.url)} -Body '{{"auth_key":"abc"}}' -DelaySeconds 0
@{{ error = $e }} | ConvertTo-Json -Compress
""")
    receiver.server.shutdown()
    assert result["error"] is None
    assert len(receiver.seen) == 2
    path, agent, body = receiver.seen[-1]
    assert path == "/tok/" and "remoteos-enroll" in agent
    assert json.loads(body) == {"auth_key": "abc"}


def test_send_reports_a_failure_without_the_body(tmp_path):
    receiver = _Receiver([404, 404])
    result = _ps(tmp_path, f"""
$e = Send-RemoteOSEnrollment -Url {_q(receiver.url)} -Body '{{"auth_key":"SecretValue"}}' -Tries 2 -DelaySeconds 0
@{{ error = $e }} | ConvertTo-Json -Compress
""")
    receiver.server.shutdown()
    assert result["error"] and "SecretValue" not in result["error"]
    assert len(receiver.seen) == 2
