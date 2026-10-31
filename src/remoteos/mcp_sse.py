"""The MCP client's limit on one reply, for the two in-repo clients (``remoteos-call``, the fleet server).

One reply is ONE server-sent event. httpx2's EventSource refuses an event over 1 MiB by default,
which a FileDownload of ~780 KB or a Shell printing 1 MB already exceeds. The largest legitimate
reply is a FileDownload of a big file (its base64), so a client allows this much.
"""

from __future__ import annotations

MAX_REPLY_EVENT_BYTES = 256 * 1024 * 1024


def allow_large_replies() -> None:
    """Lift the SDK's 1 MiB per-event limit for this process (see ``MAX_REPLY_EVENT_BYTES``).

    ``mcp.client.streamable_http`` reads a POST reply with its module-level name ``EventSource``:
    mcp 2.2 as ``EventSource(response)`` (httpx2's 1 MiB default, no option), mcp 2.3 as
    ``EventSource(response, max_event_size=self.max_sse_event_size)`` (1 MiB again: fastmcp 4.1.2
    never passes ``max_sse_event_size`` to ``streamable_http_client``). This swaps that name for
    a subclass that always uses ``MAX_REPLY_EVENT_BYTES``, which covers both. Idempotent. An SDK
    without that name raises ``RuntimeError`` here; one that stops using it fails a big reply with
    the SDK's own "exceeded the 1048576 byte limit" line (and the large-reply tests).
    """
    import httpx2
    from mcp.client import streamable_http

    current = getattr(streamable_http, "EventSource", None)
    if current is None or not (isinstance(current, type) and issubclass(current, httpx2.EventSource)):
        raise RuntimeError(
            "this mcp SDK is incompatible: mcp.client.streamable_http no longer uses httpx2.EventSource "
            "(replies over 1 MiB could not be received)"
        )
    if current is not httpx2.EventSource:
        return  # already swapped

    class LargeEventSource(httpx2.EventSource):
        def __init__(self, response, max_event_size: int | None = None) -> None:  # the SDK's value is ignored
            super().__init__(response, max_event_size=MAX_REPLY_EVENT_BYTES)

    streamable_http.EventSource = LargeEventSource
