"""Development-build access override — single toggle, zero refactor to revert.

When enabled (non-production builds), every user resolves to full PRO entitlements
so all tier-gated features are open for testing. Production leaves it OFF and the
real subscription/tier logic applies unchanged.

Design: the entitlement system stays fully intact. Only the *resolution* of a
user's effective tier is short-circuited here. Re-enabling the paywall is a single
call to ``set_unlimited_access(False)`` (or simply running with ENVIRONMENT=production,
which never turns it on). No gating code is deleted.
"""
from __future__ import annotations

_UNLIMITED_ACCESS = False


def set_unlimited_access(enabled: bool) -> None:
    """Set at the composition root from the runtime environment."""
    global _UNLIMITED_ACCESS
    _UNLIMITED_ACCESS = enabled


def unlimited_access_enabled() -> bool:
    return _UNLIMITED_ACCESS
