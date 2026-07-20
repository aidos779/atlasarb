"""Admin subscription management: grant/revoke and the read-only views.

The service layer is tested directly; the handlers are thin wrappers over it whose
only extra responsibility is the ADMIN-role gate, asserted separately below.
"""
import pytest

from src.bot.handlers.admin import _is_admin
from src.database.repositories.misc_repos import AuditRepository
from src.domain.enums import SubscriptionTier, UserRole
from src.domain.user import UserProfile
from src.services.admin_service import AdminService
from src.services.payments import PlaceholderPaymentProvider
from src.services.purchase_service import PurchaseService
from src.services.signal_access_service import CHANNEL_LIST, SignalAccessService
from src.services.subscription_service import SubscriptionService
from tests.integration.test_subscription import _make_user, _reload, _signal

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999


# ── role gate ──

def test_only_administrators_pass_the_gate():
    assert _is_admin(UserProfile(telegram_user_id=1, role=UserRole.ADMIN)) is True
    # Support can read tickets but must not move paid access.
    assert _is_admin(UserProfile(telegram_user_id=2, role=UserRole.SUPPORT)) is False
    assert _is_admin(UserProfile(telegram_user_id=3, role=UserRole.PAID)) is False
    assert _is_admin(UserProfile(telegram_user_id=4, role=UserRole.FREE)) is False


# ── grant / revoke ──

async def test_grant_pro_activates_lifetime(database):
    await _make_user(database, uid=5)
    admin = AdminService(database, None)
    assert await admin.grant_pro(ADMIN_ID, 5, "vip") is True

    profile = await _reload(database, uid=5)
    assert profile.subscription.tier == SubscriptionTier.PRO_LIFETIME
    assert profile.effective_tier == SubscriptionTier.PRO_LIFETIME
    assert profile.role == UserRole.PAID
    assert profile.subscription.purchased_at is not None


async def test_revoke_pro_returns_user_to_free(database):
    await _make_user(database, uid=5)
    admin = AdminService(database, None)
    await admin.grant_pro(ADMIN_ID, 5, "vip")
    assert await admin.revoke_pro(ADMIN_ID, 5, "chargeback") is True

    profile = await _reload(database, uid=5)
    assert profile.subscription.tier == SubscriptionTier.FREE
    assert profile.effective_tier == SubscriptionTier.FREE
    assert profile.role == UserRole.FREE


async def test_revoke_does_not_refill_the_free_quota(database, settings, catalog):
    """A user who already spent their 5 signals stays paywalled after a revoke —
    the delivery ledger is history, not a balance that a tier change resets."""
    profile = await _make_user(database, uid=5)
    access = SignalAccessService(database, catalog)
    await access.record_deliveries(profile, [_signal().id for _ in range(5)], CHANNEL_LIST)

    admin = AdminService(database, None)
    await admin.grant_pro(ADMIN_ID, 5, "vip")
    await admin.revoke_pro(ADMIN_ID, 5, "revoked")

    reverted = await _reload(database, uid=5)
    allowance = await access.allowance(reverted)
    assert allowance.delivered == 5
    assert allowance.exhausted is True


async def test_grant_and_revoke_are_audited(database):
    await _make_user(database, uid=5)
    admin = AdminService(database, None)
    await admin.grant_pro(ADMIN_ID, 5, "vip access")
    await admin.revoke_pro(ADMIN_ID, 5, "refund issued")

    async with database.session() as session:
        entries = await AuditRepository(session).for_user(5)
    actions = [e.action for e in entries]
    assert actions.count("tier_override") == 2
    assert {"vip access", "refund issued"} <= {e.reason for e in entries}


async def test_grant_to_unknown_user_fails_cleanly(database):
    assert await AdminService(database, None).grant_pro(ADMIN_ID, 4040, "x") is False


async def test_admin_grant_is_not_counted_as_revenue(database):
    await _make_user(database, uid=5)
    admin = AdminService(database, None)
    await admin.grant_pro(ADMIN_ID, 5, "vip")
    assert await admin.revenue_usd() == 0.0


# ── read views ──

async def test_subscription_view_data(database, settings, catalog):
    """What /subscription <id> renders: tier, delivered signals, purchase state."""
    profile = await _make_user(database, uid=5)
    access = SignalAccessService(database, catalog)
    await access.record_deliveries(profile, [_signal().id for _ in range(2)], CHANNEL_LIST)

    allowance = await access.allowance(await _reload(database, uid=5))
    assert allowance.delivered == 2
    assert allowance.quota == 5
    assert allowance.exhausted is False

    purchases = PurchaseService(database, SubscriptionService(database, settings, catalog),
                                PlaceholderPaymentProvider())
    assert await purchases.latest(5) is None       # nothing bought yet


async def test_payments_view_lists_history(database, settings, catalog):
    await _make_user(database, uid=5)
    purchases = PurchaseService(database, SubscriptionService(database, settings, catalog),
                                PlaceholderPaymentProvider())
    # The placeholder cannot issue an invoice, so this records an attempt, not a sale.
    await purchases.start_checkout(5, catalog.pro_lifetime())

    history = await purchases.history(5)
    assert len(history) == 1
    assert history[0].product_code.value == "PRO_LIFETIME"
    assert history[0].status.value == "cancelled"
    assert await AdminService(database, None).revenue_usd() == 0.0
