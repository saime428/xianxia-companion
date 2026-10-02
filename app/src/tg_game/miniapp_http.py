"""A workflow owns its connection pool and only retries before HTTP transmission."""
from contextlib import contextmanager

import httpx


@contextmanager
def pooled_miniapp_transport(*, timeout: float, origin: str, referer: str = ""):
    # Own this context inside the synchronous worker, so cancelling its awaiter
    # cannot close connections while that worker is still handling a request.
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=300),
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Origin": origin,
                 "Referer": referer or origin + "/"},
    ) as client:
        def send(request):
            for attempt in range(2):
                try:
                    response = client.request(request.get("method") or "POST", request["url"],
                                              json=request.get("payload") or {})
                    return response.status_code, response.content
                except (httpx.ConnectTimeout, httpx.ConnectError):
                    # These exceptions occur before the HTTP request is sent.
                    # Read/write failures can be accepted writes: let the caller
                    # reconcile them instead of repeating a start or settlement.
                    if attempt:
                        raise
        yield send
