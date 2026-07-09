"""Structured transport errors shared by all exchange adapters.

A non-200 HTTP response is a *transport* outcome, not a JSON document — adapters must
never attempt to JSON-decode an error body (the old ``rest_json_decode_failed`` noise).
``RestError`` carries the real HTTP status plus enough context (venue, URL, body prefix)
for the log line to name the exact cause without a stack trace.
"""
from __future__ import annotations


class RestError(Exception):
    """HTTP request failed (non-200) or returned a non-JSON body on 200."""

    def __init__(self, venue: str, url: str, status: int, message: str,
                 body_prefix: str = "", retry_after: str | None = None) -> None:
        super().__init__(f"{venue}: HTTP {status} — {message}")
        self.venue = venue
        self.url = url
        self.status = status
        self.message = message
        self.body_prefix = body_prefix
        self.retry_after = retry_after

    @property
    def blocked(self) -> bool:
        """True for statuses that mean this host/IP is blocked (geo/CDN), where
        retrying the same route is pointless and a host/proxy switch is required."""
        return self.status in (403, 451)
