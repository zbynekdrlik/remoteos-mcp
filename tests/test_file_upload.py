"""FileUpload: a size cap and strict base64, both checked before anything is written (#31).

The real tool writes into pytest's ``tmp_path``; nothing is faked.
"""

from __future__ import annotations

import base64

import remoteos.__main__ as server

_CAP = 100 * 1024 * 1024  # 100 MB of base64 text, as upstream winremote c3a675174c


def test_upload_writes_the_decoded_bytes_and_creates_the_directory(tmp_path):
    dest = tmp_path / "sub" / "f.bin"
    payload = bytes(range(256))
    assert server.FileUpload(str(dest), base64.b64encode(payload).decode()) == f"Written 256 bytes to {dest}"
    assert dest.read_bytes() == payload


def test_line_wrapped_base64_is_accepted(tmp_path):
    # `base64` wraps its output at 76 columns; the line breaks are formatting, not data
    dest = tmp_path / "wrapped.bin"
    payload = bytes(range(256)) * 2
    encoded = base64.encodebytes(payload).decode()
    assert "\n" in encoded.strip()
    assert server.FileUpload(str(dest), encoded) == f"Written 512 bytes to {dest}"
    assert dest.read_bytes() == payload


def test_non_base64_is_rejected_and_the_existing_file_is_untouched(tmp_path):
    dest = tmp_path / "keep.txt"
    dest.write_bytes(b"precious")
    out = server.FileUpload(str(dest), "!!!!")
    assert out.startswith("FileUpload error: data_base64 is not valid base64")
    assert dest.read_bytes() == b"precious"


def test_stray_characters_are_rejected_not_skipped(tmp_path):
    dest = tmp_path / "x.bin"
    out = server.FileUpload(str(dest), "aGVs*bG8=")  # lax decoding silently dropped '*' and wrote b"hello"
    assert out.startswith("FileUpload error: data_base64 is not valid base64")
    assert not dest.exists()


def test_rejected_input_creates_no_directory(tmp_path):
    server.FileUpload(str(tmp_path / "new" / "x.bin"), "!!!!")
    assert not (tmp_path / "new").exists()


def test_over_the_cap_is_rejected_before_decoding(tmp_path):
    dest = tmp_path / "big.bin"
    out = server.FileUpload(str(dest), "A" * (_CAP + 4))
    assert out == "FileUpload error: data_base64 is 104857604 characters; the limit is 104857600 (100 MB of base64)"
    assert not dest.exists()


def test_exactly_the_cap_is_accepted(tmp_path):
    dest = tmp_path / "max.bin"
    out = server.FileUpload(str(dest), "A" * _CAP)
    assert out == f"Written {_CAP // 4 * 3} bytes to {dest}"
    assert dest.stat().st_size == _CAP // 4 * 3
    dest.unlink()
