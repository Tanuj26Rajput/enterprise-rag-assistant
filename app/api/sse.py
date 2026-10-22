"""Server-sent event framing.

SSE rather than WebSockets: the traffic is one-directional, it survives
proxies that only understand HTTP, and it reconnects on its own in every
browser. A WebSocket would add a second protocol, a second auth path and a
second set of failure modes for no gain here.
"""

from __future__ import annotations

import json
from typing import Any

#: Sent periodically so that proxies with an idle timeout do not cut a
#: stream that is merely waiting for the first token. A line beginning with
#: ':' is a comment in the SSE grammar and is ignored by every client.
KEEPALIVE = ": keep-alive\n\n"

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Nginx buffers proxied responses by default, which holds every token
    # until the response ends - turning a stream into a slow request.
    "X-Accel-Buffering": "no",
}


def event(name: str, data: Any) -> str:
    """Frame one named event carrying a JSON payload.

    The payload is serialised compactly and newline-escaped by ``json.dumps``,
    which matters: a raw newline inside ``data:`` would terminate the field
    and split one event into two malformed ones.
    """
    body = json.dumps(data, separators=(",", ":"), default=str)
    return f"event: {name}\ndata: {body}\n\n"
