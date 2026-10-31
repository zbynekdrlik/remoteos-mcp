"""File tools: read, write, list, search, and the binary transfer ones."""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

try:
    from mcp.types import ToolAnnotations
except ImportError:
    from fastmcp.tools import ToolAnnotations

from remoteos.app import _tobool, log, mcp
from remoteos.errors import failure, reports_errors
from remoteos.file_transfer import hash_file, replace_file, start_file_like


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileRead",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileRead(path: str, encoding: str = "utf-8") -> str:
    """Read file content. Returns base64 for binary files.

    Args:
        path: File path.
        encoding: Text encoding (default utf-8). Use 'binary' for base64 output.
    """
    p = Path(path)
    if not p.exists():
        raise failure(f"File not found: {path}")
    if encoding == "binary":
        data = p.read_bytes()
        return base64.b64encode(data).decode()
    text = p.read_text(encoding=encoding, errors="replace")
    if len(text) > 100000:
        text = text[:100000] + "\n\n[... truncated at 100KB]"
    return text


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileWrite",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileWrite(path: str, content: str, encoding: str = "utf-8", append: bool | str = False) -> str:
    """Write content to a file.

    Args:
        path: File path.
        content: Content to write.
        encoding: Text encoding (default utf-8).
        append: Append instead of overwrite.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if _tobool(append) else "w"
    with open(p, mode, encoding=encoding) as f:
        f.write(content)
    return f"Written {len(content)} chars to {path}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileList",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileList(path: str = ".", show_hidden: bool | str = False) -> str:
    """List directory contents with size and modification date.

    Args:
        path: Directory path.
        show_hidden: Include hidden files/folders.
    """
    from tabulate import tabulate

    p = Path(path)
    if not p.is_dir():
        raise failure(f"Not a directory: {path}")

    rows = []
    for item in sorted(p.iterdir()):
        name = item.name
        if not _tobool(show_hidden) and name.startswith("."):
            continue
        try:
            stat = item.stat()
            size = stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            kind = "DIR" if item.is_dir() else "FILE"
            if item.is_dir():
                size_str = "<DIR>"
            elif size < 1024:
                size_str = f"{size}B"
            elif size < 1048576:
                size_str = f"{size // 1024}KB"
            else:
                size_str = f"{size // 1048576}MB"
            rows.append([kind, name, size_str, mtime])
        except (OSError, ValueError, OverflowError):  # one unreadable entry or out-of-range mtime: "?"
            rows.append(["?", name, "?", "?"])

    if not rows:
        return "Directory is empty."
    return tabulate(rows, headers=["Type", "Name", "Size", "Modified"], tablefmt="simple")


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileSearch",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileSearch(pattern: str, path: str = ".", recursive: bool | str = True, limit: int = 50) -> str:
    """Search files by name pattern.

    Args:
        pattern: Glob pattern (e.g. '*.py', 'report*').
        path: Root directory to search.
        recursive: Search subdirectories.
        limit: Max results.
    """
    p = Path(path)
    glob_iter = p.rglob(pattern) if _tobool(recursive) else p.glob(pattern)

    # Stream results — stop as soon as we have enough (don't scan entire disk)
    lines = []
    total = 0
    for m in glob_iter:
        total += 1
        if len(lines) < limit:
            try:
                size = m.stat().st_size
                lines.append(f"  {m} ({size} bytes)")
            except OSError:  # a match that vanished or cannot be stat'ed is listed without its size
                lines.append(f"  {m}")
        if len(lines) >= limit:
            break  # Don't keep scanning after hitting limit

    if not lines:
        return f"No files matching '{pattern}' in {path}"

    result = f"Found {total}+ files (showing first {limit})"
    result += ":\n" + "\n".join(lines)
    return result


# ========================== FILE TRANSFER (BINARY) =========================


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileDownload",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileDownload(path: str) -> str:
    """Download a file as base64-encoded content. Use for binary files.

    Args:
        path: File path to download.
    """
    p = Path(path)
    if not p.exists():
        raise failure(f"File not found: {path}")
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode()
    return f"base64:{len(data)}bytes:{b64}"


# FileUpload cap on the base64 TEXT (~75 MB of file), checked before decoding (#31).
FILE_UPLOAD_MAX_BASE64 = 100 * 1024 * 1024
_B64_WHITESPACE = str.maketrans("", "", " \t\r\n")  # line-wrapped base64 (e.g. `base64` output) is fine


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileUpload",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileUpload(path: str, data_base64: str, append: bool = False, permissions_from: str = "") -> str:
    """Upload a file from base64-encoded content. Use for binary files.

    Over HTTP one request carries at most 4 MiB, so one call carries about 3 MB of file. Send a
    larger file in pieces: ``append=False`` first, then ``append=True``, ideally to a temporary
    name, checked with FileHash and moved into place with FileRename (``remoteos-call put`` does
    exactly that).

    Args:
        path: Destination file path.
        data_base64: Base64-encoded file content, at most 100 MB of base64 text. Line breaks are
            allowed; any other character outside the base64 alphabet is an error and nothing is
            written.
        append: Add to the end of the file instead of replacing it (a missing file is created).
        permissions_from: Without append: start a new file with this existing file's permissions,
            so a temporary copy of a private file is private too. A directory here is an error.
    """
    # Both checks run before the directory is created or the file opened (#31):
    # lax decoding dropped junk characters, so "!!!!" truncated a file to 0 bytes.
    if len(data_base64) > FILE_UPLOAD_MAX_BASE64:
        log.warning("FileUpload refused: %d base64 characters over the cap", len(data_base64))  # never the path
        raise ValueError(
            f"data_base64 is {len(data_base64)} characters; the limit is {FILE_UPLOAD_MAX_BASE64} (100 MB of base64)"
        )
    try:
        data = base64.b64decode(data_base64.translate(_B64_WHITESPACE), validate=True)
    except ValueError as e:  # binascii.Error is one; non-ASCII text raises a plain ValueError
        log.warning("FileUpload refused: invalid base64 (%s)", e)
        raise ValueError(f"data_base64 is not valid base64 ({e})") from e
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if permissions_from and not append:
        start_file_like(p, permissions_from)
    with p.open("ab" if append else "wb") as handle:
        handle.write(data)
    log.info("FileUpload wrote %d bytes", len(data))  # never the path: logs carry no tool arguments (#31)
    return f"Written {len(data)} bytes to {path}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileHash",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileHash(path: str) -> str:
    """The SHA-256 and size of a file: ``sha256:<hex> <size> bytes``. Use it to check a transfer.

    Args:
        path: File path.
    """
    p = Path(path)
    if not p.exists():
        raise failure(f"File not found: {path}")
    digest, size = hash_file(p)
    log.info("FileHash read %d bytes", size)  # never the path (#31)
    return f"sha256:{digest} {size} bytes"


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileRename",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def FileRename(path: str, new_path: str) -> str:
    """Rename a file, replacing ``new_path`` in one step if it exists.

    The new file takes an existing ``new_path``'s POSIX mode and group (and, when the server runs
    as root, its owner), not its ACLs or extended attributes. A symlink there is written through,
    a ``new_path`` this account may not write is refused. Both paths must be on the same filesystem.

    Args:
        path: The file to rename.
        new_path: Its new path.
    """
    p = Path(path)
    if not p.exists():
        raise failure(f"File not found: {path}")
    replace_file(p, new_path)
    log.info("FileRename replaced a file")  # never the paths (#31)
    return f"Renamed {path} to {new_path}"
