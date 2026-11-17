"""The README one-liners (#101): enroll-linux.sh / enroll.sh run the normal installer with the permanent
endpoint baked in, and post the install log to dev1 when it fails.

The generated wrappers run whole under bash (and, in the shell CI job, under macOS's bash 3.2: no
conftest, so this file needs none). Only the network is faked: a `curl` on PATH that serves a fake
installer for the download and records each POST. enroll.ps1 runs in real PowerShell in
tests_windows/test_enroll_ps1.py; its static shape is pinned here.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = os.environ.get("INSTALLER_TEST_BASH", "bash")
VALUES = dict(line.split("=", 1) for line in (ROOT / "installers" / "enroll-values.conf").read_text().splitlines()
              if line and not line.startswith("#"))

FAKE_CURL = """#!{python}
import json, os, shutil, sys
args = sys.argv[1:]
record = {{"args": args}}
if "-o" in args:
    record["download"] = args[-1]
    with open(os.environ["FAKE_CURL_LOG"], "a") as log:
        log.write(json.dumps(record) + "\\n")
    if os.environ.get("FAKE_DOWNLOAD_FAILS"):
        print("curl: (22) The requested URL returned error: 404", file=sys.stderr)
        sys.exit(22)
    shutil.copy(os.environ["FAKE_INSTALLER"], args[args.index("-o") + 1])
    sys.exit(0)
headers = [args[i + 1] for i, a in enumerate(args) if a == "-H"]
record.update(url=args[-1], headers=headers, body=sys.stdin.read())
with open(os.environ["FAKE_CURL_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
print("ok", end="")
"""

FAKE_INSTALLER = """\
echo "enroll=$REMOTEOS_ENROLL"
echo "sshkey=$REMOTEOS_ENROLL_SSH_KEY"
echo "ref=$REMOTEOS_MCP_REF"
if read -r line; then echo "stdin was not empty: $line"; fi
echo "  [X] pip failed: no space left" >&2
exit "${FAKE_INSTALL_RC:-0}"
"""


class Box:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "curl.jsonl"
        self.installer = tmp_path / "fake-installer.sh"
        self.installer.write_text(FAKE_INSTALLER)
        curl = self.bin / "curl"
        curl.write_text(FAKE_CURL.format(python=sys.executable))
        hostname = self.bin / "hostname"
        hostname.write_text("#!/bin/sh\necho predaj3.local\n")
        for path in (curl, hostname):
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess:
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                "FAKE_CURL_LOG": str(self.log), "FAKE_INSTALLER": str(self.installer)}
        # piped like `curl ... | bash`: the script comes on stdin
        return subprocess.run([BASH], input=(ROOT / script).read_text(), capture_output=True, text=True,
                              env={**base, **env}, timeout=60)

    def requests(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


@pytest.mark.parametrize("script, installer", [("enroll-linux.sh", "install-linux.sh"), ("enroll.sh", "install.sh")])
def test_the_wrapper_runs_the_installer_with_the_baked_endpoint_and_key(box, script, installer):
    result = box.run(script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"enroll={VALUES['ENROLL_URL']}" in result.stdout
    assert f"sshkey={VALUES['ENROLL_SSH_KEY']}" in result.stdout
    assert "ref=main" in result.stdout
    assert "stdin was not empty" not in result.stdout  # the rest of the piped script never reaches it
    [download] = box.requests()
    assert download["download"] == f"https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/{installer}"
    assert "--proto" in download["args"] and "=https" in download["args"]
    assert "Done: dev1 has this box's registration" in result.stdout


@pytest.mark.parametrize("script, os_name", [("enroll-linux.sh", "linux"), ("enroll.sh", "macos")])
def test_a_failed_install_posts_its_log_to_dev1_and_keeps_the_exit_code(box, script, os_name):
    result = box.run(script, FAKE_INSTALL_RC="3")
    assert result.returncode == 3
    _, post = box.requests()
    assert post["url"] == VALUES["ENROLL_URL"] + "log"
    assert "X-Remoteos-Host: predaj3" in post["headers"]
    assert f"X-Remoteos-Os: {os_name}" in post["headers"]
    assert "X-Remoteos-Exit: 3" in post["headers"]
    assert "pip failed: no space left" in post["body"]  # stderr is in the log too
    assert "The install log went to dev1." in result.stdout


def test_a_failed_download_posts_the_reason_and_changes_nothing(box):
    result = box.run("enroll-linux.sh", FAKE_DOWNLOAD_FAILS="1")
    assert result.returncode == 1
    assert "nothing was changed on this box" in result.stdout
    _, post = box.requests()
    assert "X-Remoteos-Exit: 1" in post["headers"] and "404" in post["body"]


def test_a_ref_that_is_not_plain_is_refused_before_any_download(box):
    result = box.run("enroll-linux.sh", REMOTEOS_MCP_REF="main;rm -rf /")
    assert result.returncode == 1 and box.requests() == []


def test_a_branch_ref_is_used_for_the_download_and_the_install(box):
    result = box.run("enroll-linux.sh", REMOTEOS_MCP_REF="dev")
    assert result.returncode == 0 and "ref=dev" in result.stdout
    assert box.requests()[0]["download"].endswith("/dev/install-linux.sh")


def test_the_baked_url_passes_the_installers_own_enrollment_url_check():
    # install-linux.sh's enroll_url_ok and install.ps1's Test-RemoteOSEnrollUrl: one path segment + '/'
    assert re.fullmatch(r"https://[a-z0-9.-]+/[A-Za-z0-9_-]+/", VALUES["ENROLL_URL"])
    assert re.fullmatch(r"(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+", VALUES["ENROLL_SSH_KEY"])


def test_enroll_ps1_is_ascii_has_the_values_and_no_placeholder():
    text = (ROOT / "enroll.ps1").read_bytes()
    text.decode("ascii")  # Windows PowerShell 5.1 reads a BOM-less file in the ANSI page
    source = text.decode()
    assert f"$EnrollUrl = '{VALUES['ENROLL_URL']}'" in source
    assert f"$EnrollSshKey = '{VALUES['ENROLL_SSH_KEY']}'" in source
    assert "@@" not in source
    for token in ("??", "?.", "&&", "||"):
        assert token not in source
    # the flow is the last statement: a truncated download defines functions but runs nothing
    assert source.rstrip().endswith("$global:LASTEXITCODE = $enrollExit")
