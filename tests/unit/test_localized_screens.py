"""Every rendered screen is fully localized, and switching language takes effect at once.

The catalog tests prove the *strings* are complete. These prove the *screens* actually
use them: each surface is rendered in both languages and checked for three failures that
a completeness test cannot see —

  * a ``⟦key⟧`` marker, meaning a handler asked for a key that does not exist;
  * English prose surviving into a Russian render (the old signal card hardcoded
    "Buy:", "Liquidity:", "Risk:" and ignored the language argument entirely);
  * a render that is byte-identical across languages, meaning the language argument was
    accepted and then dropped.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from src.bot.formatters.signal import format_alert, format_card, format_details
from src.bot.keyboards.inline import (
    details_buttons,
    main_menu,
    signal_card_buttons,
    upsell_keyboard,
)
from src.bot.keyboards.reply import main_reply_keyboard
from src.bot.keyboards.screens import (
    admin_menu,
    favorites_hub,
    filters_panel,
    notifications_menu,
    settings_menu,
    subscription_menu,
    support_menu,
)
from src.domain.entitlements import entitlements_for
from src.domain.enums import ArbitrageType, Language, RiskScore
from src.domain.signal import Signal
from src.domain.user import UserProfile
from src.i18n import LANGUAGES, t

MARKER = re.compile(r"⟦[^⟧]+⟧")
# Prose that appeared verbatim on the old hardcoded screens. None of it may survive a
# Russian render. Deliberately excludes proper nouns (Binance, USDT, Pro…).
ENGLISH_PROSE = [
    "Buy:", "Sell:", "Liquidity:", "Risk:", "Active ", "Gross Spread",
    "Profit Breakdown", "Trading fees", "Withdrawal", "Network/gas",
    "Recommended size", "Trade Route", "No transfer required", "Historical Performance",
    "Signal ID", "Status:", "Detected:", "Confidence:", "Low confidence",
    "Minimum Profit", "Save as Default", "Reset", "Language:", "Timezone:", "Currency:",
    "User Management", "Broadcast Messages", "Signal Monitoring", "Support Queue",
    "Plan Comparison", "Upgrade", "Favorite Exchanges", "Notification Preferences",
    "Tomorrow", "Type value", "Done",
]


class _Fx:
    """Identity FX — format_money awaits a rate lookup; the value itself is irrelevant."""

    async def convert(self, amount, currency):  # noqa: D102
        return amount

    async def rate(self, currency):  # noqa: D102
        return Decimal(1)


def _profile(lang: str) -> UserProfile:
    profile = UserProfile(telegram_user_id=1, username="tester", first_name="Test")
    profile.settings.language = Language(lang)
    return profile


def _signal(arb_type: ArbitrageType = ArbitrageType.CEX_CEX) -> Signal:
    return Signal(
        arb_type=arb_type, coin="BTC", trading_pair="BTC/USDT", network="ethereum",
        buy_exchange="binance", sell_exchange="okx",
        buy_price=Decimal("100"), sell_price=Decimal("102"),
        buy_venue_type="CEX", sell_venue_type="CEX",
        spread_pct=Decimal("2.0"), net_profit_pct=Decimal("1.5"),
        net_profit_usd=Decimal("15"), liquidity_usd=Decimal("50000"),
        risk_score=RiskScore.MEDIUM, confidence_score=88.0,
    )


def _keyboard_text(markup) -> str:
    return "\n".join(btn.text for row in markup.inline_keyboard for btn in row)


def _assert_localized(rendered: str, lang: str, what: str) -> None:
    assert not MARKER.search(rendered), f"{what} [{lang}] has a missing key: {rendered}"
    if lang == "ru":
        leaked = [p for p in ENGLISH_PROSE if p in rendered]
        assert not leaked, f"{what} [ru] leaked English: {leaked}\n{rendered}"


# ── inline keyboards ──
@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("builder", [
    main_menu, favorites_hub, support_menu, admin_menu, upsell_keyboard,
])
def test_simple_keyboards_are_localized(builder, lang):
    _assert_localized(_keyboard_text(builder(lang)), lang, builder.__name__)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_profile_keyboards_are_localized(lang):
    profile = _profile(lang)
    ent = entitlements_for(profile.effective_tier)
    for name, markup in [
        ("settings_menu", settings_menu(profile, lang)),
        ("subscription_menu", subscription_menu(profile, lang)),
        ("filters_panel", filters_panel(profile, ent, lang)),
        ("notifications_menu", notifications_menu(profile, ent, lang)),
    ]:
        _assert_localized(_keyboard_text(markup), lang, name)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_signal_keyboards_are_localized(lang):
    signal = _signal()
    _assert_localized(_keyboard_text(signal_card_buttons(signal, False, lang)),
                      lang, "signal_card_buttons")
    _assert_localized(_keyboard_text(details_buttons(signal, True, "bot", lang)),
                      lang, "details_buttons")


def test_reply_keyboard_is_localized():
    for lang in LANGUAGES:
        text = "\n".join(b.text for row in main_reply_keyboard(lang).keyboard for b in row)
        _assert_localized(text, lang, "main_reply_keyboard")


# ── rendered message bodies ──
@pytest.mark.parametrize("lang", LANGUAGES)
async def test_signal_card_is_localized(lang):
    rendered = await format_card(_signal(), _profile(lang), _Fx())
    _assert_localized(rendered, lang, "format_card")


@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("arb_type", list(ArbitrageType))
async def test_signal_details_are_localized(lang, arb_type):
    rendered = await format_details(_signal(arb_type), _profile(lang), _Fx(), reliability=91.0)
    _assert_localized(rendered, lang, f"format_details[{arb_type.value}]")


@pytest.mark.parametrize("lang", LANGUAGES)
async def test_alert_is_localized(lang):
    rendered = await format_alert(_signal(), _profile(lang), _Fx())
    _assert_localized(rendered, lang, "format_alert")


# ── the language argument is honoured, not accepted and ignored ──
async def test_signal_card_actually_differs_between_languages():
    """format_card used to take `lang`, discard it (`_ = lang`), and emit English."""
    en = await format_card(_signal(), _profile("en"), _Fx())
    ru = await format_card(_signal(), _profile("ru"), _Fx())
    assert en != ru


async def test_signal_details_actually_differ_between_languages():
    en = await format_details(_signal(), _profile("en"), _Fx())
    ru = await format_details(_signal(), _profile("ru"), _Fx())
    assert en != ru


@pytest.mark.parametrize("builder", [main_menu, admin_menu, favorites_hub, support_menu])
def test_keyboards_actually_differ_between_languages(builder):
    assert _keyboard_text(builder("en")) != _keyboard_text(builder("ru"))


# ── switching language re-renders immediately (§14, no restart) ──
def test_switching_language_changes_every_menu_at_once():
    """A profile flipped to ru must render ru on the very next build — the menus are
    built per-render from profile.settings.language, so there is no cached copy to stale."""
    profile = _profile("en")
    ent = entitlements_for(profile.effective_tier)
    before = _keyboard_text(settings_menu(profile, profile.settings.language.value))
    assert t("settings.fav_coins", "en") in before

    profile.settings.language = Language.RU          # what set_language persists

    lang = profile.settings.language.value
    for markup in (settings_menu(profile, lang), main_menu(lang),
                   filters_panel(profile, ent, lang), notifications_menu(profile, ent, lang)):
        _assert_localized(_keyboard_text(markup), "ru", "post-switch render")
    assert t("settings.fav_coins", "ru") in _keyboard_text(settings_menu(profile, lang))


def test_switching_language_changes_the_reply_keyboard():
    """The persistent keyboard is the one surface Telegram caches client-side, so the
    handler must resend it on switch — proven here by it differing per language."""
    en = [b.text for row in main_reply_keyboard("en").keyboard for b in row]
    ru = [b.text for row in main_reply_keyboard("ru").keyboard for b in row]
    assert en != ru
    assert ru == [t("menu.signals", "ru"), t("menu.favorites", "ru"),
                  t("menu.subscription", "ru"), t("menu.settings", "ru")]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
