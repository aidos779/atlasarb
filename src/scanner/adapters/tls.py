"""Shared TLS context for all outbound HTTPS/WSS (adapters, RPC, FX).

aiohttp's default connector builds an ``ssl.SSLContext`` from the *system* trust store
(``ssl.create_default_context()``). On several supported runtimes that store is empty or
incomplete — a stock python.org macOS build (the "Install Certificates.command" step
never run) and slim/distroless Docker images without the ``ca-certificates`` package —
which makes every exchange REST call, DEX RPC call and WebSocket handshake fail with
``CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`` / ``unable to
get local issuer certificate``. With no venue reachable, no market data enters the cache
and *no* detector can ever emit a signal.

Binding the trust store to the bundled ``certifi`` CA set makes verification deterministic
across environments (this is the same bundle pip/requests rely on) while keeping full
certificate verification on — we do NOT disable verification.
"""
from __future__ import annotations

import ssl

import certifi

_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """Process-wide verified TLS context backed by the certifi CA bundle."""
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = ssl.create_default_context(cafile=certifi.where())
    return _CONTEXT
