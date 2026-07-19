"""Signal rendering — compact card (§9.3) and full Details (§10).

Respects the user's Currency and Timezone (BR-DETAILS-3) and tier gating: Free sees
Overview/Profit/Fees/Liquidity; Historical Performance & Trade Route are Pro-only,
rendered as a locked preview for Free (BR-DETAILS-2).

Every label here is localized (FR-LOC-01). Only proper nouns pass through untranslated:
exchange names, tickers, network names, and the signal id.
"""
from __future__ import annotations

import html

from src.bot.formatters.money import format_datetime, format_money, format_pct
from src.domain.entitlements import entitlements_for
from src.domain.enums import ArbitrageType, SignalStatus
from src.domain.signal import Signal
from src.domain.user import UserProfile
from src.i18n import arb_type_label, risk_explanation, risk_label, status_label, t

_CONF_THRESHOLD = 70


async def format_card(signal: Signal, profile: UserProfile, fx,
                      confidence_threshold: float = _CONF_THRESHOLD) -> str:
    lang = profile.settings.language.value
    cur = profile.settings.currency.value
    net_usd = await format_money(fx, signal.net_profit_usd, cur)
    pair = html.escape(signal.trading_pair)
    network = signal.network or t("profile.unknown", lang)
    liq = await format_money(fx, signal.liquidity_usd, cur)
    lines = [
        f"{signal.ranking.emoji} <b>{pair}</b>  {format_pct(signal.net_profit_pct)} (~{net_usd})",
        t("card.route", lang, buy=html.escape(signal.buy_exchange),
          sell=html.escape(signal.sell_exchange)),
        t("card.meta", lang, liquidity=liq, network=network,
          risk_emoji=signal.risk_score.emoji, risk=risk_label(signal.risk_score, lang)),
        t("card.active", lang, seconds=signal.age_sec()),
    ]
    if signal.is_low_confidence(confidence_threshold):
        lines.append(t("card.low_confidence", lang))
    return "\n".join(lines)


async def format_details(signal: Signal, profile: UserProfile, fx,
                         reliability: float | None = None) -> str:
    lang = profile.settings.language.value
    cur = profile.settings.currency.value
    tz = profile.settings.timezone
    ent = entitlements_for(profile.effective_tier)
    pair = html.escape(signal.trading_pair)
    status_emoji = "🟢" if signal.status == SignalStatus.ACTIVE else "🔴"

    parts: list[str] = []
    if signal.status == SignalStatus.EXPIRED:
        ts = format_datetime(signal.expired_at or signal.last_updated, tz)
        parts.append(t("details.expired", lang, ts=ts))

    # ── Overview (§10.1) ──
    buy_price = await format_money(fx, signal.buy_price, cur)
    sell_price = await format_money(fx, signal.sell_price, cur)
    buy_venue = html.escape(signal.buy_exchange)
    sell_venue = html.escape(signal.sell_exchange)
    parts.append(
        f"<b>{pair}</b> — {arb_type_label(signal.arb_type, lang)} {signal.ranking.emoji}\n"
        f"{t('details.signal_id', lang)}: <code>{signal.id[:8]}</code> · "
        f"{t('details.status', lang)}: {status_emoji} {status_label(signal.status, lang)}\n"
        f"{t('details.detected', lang)}: {format_datetime(signal.timestamp, tz)} · "
        f"{t('card.active', lang, seconds=signal.age_sec())}\n\n"
        f"{t('details.buy_on', lang, exchange=buy_venue, price=buy_price)}\n"
        f"{t('details.sell_on', lang, exchange=sell_venue, price=sell_price)}\n\n"
        f"{_spread_line(signal, lang)} · "
        f"{t('details.confidence', lang)}: {signal.confidence_score}%"
    )

    # ── Profit Breakdown (§10.2) ──
    bd = signal.profit_breakdown
    if bd is not None:
        net_usd = await format_money(fx, bd.net_profit_usd, cur)
        network_fees = bd.withdrawal_fees_usd + bd.gas_fees_usd + bd.bridge_fees_usd
        # For funding, gross_spread_pct is the ANNUALIZED rate differential, not a price
        # gross to be netted against fees — label it so it is never read as the profit.
        spread_label = t(
            "details.funding_annualized" if signal.arb_type == ArbitrageType.FUNDING
            else "details.gross_spread", lang)
        parts.append(
            f"<b>{t('details.breakdown_title', lang)}</b>\n"
            f"{spread_label}: {format_pct(bd.gross_spread_pct)}\n"
            f"{t('details.trading_fees', lang)}: "
            f"−{await format_money(fx, bd.trading_fees_usd, cur)}\n"
            f"{t('details.withdrawal_network', lang)}: "
            f"−{await format_money(fx, network_fees, cur)}\n"
            f"{t('details.slippage', lang)}: "
            f"−{await format_money(fx, bd.slippage_cost_usd, cur)}\n"
            f"<b>{t('details.net_profit', lang)}: "
            f"{format_pct(bd.net_profit_pct)} (~{net_usd})</b>"
        )

    # ── Fees (§10.3) ──
    parts.append(
        f"<b>{t('details.fees_title', lang)}</b>\n"
        f"{t('details.fees_trading', lang)}: "
        f"{await format_money(fx, signal.trading_fees, cur)}\n"
        f"{t('details.fees_withdrawal', lang)}: "
        f"{await format_money(fx, signal.withdrawal_fees, cur)}\n"
        f"{t('details.fees_network', lang)}: "
        f"{await format_money(fx, signal.network_fees, cur)}"
    )

    # ── Liquidity + sizing (§10.4 / §8.12) ──
    if signal.sizing is not None:
        sz = signal.sizing
        decay = " · ".join(f"{int(p)}%→{r:.2f}%" for p, r in sz.profit_decay)
        parts.append(
            f"<b>{t('details.liquidity_title', lang)}</b>\n"
            f"{t('details.recommended_size', lang)}: "
            f"{await format_money(fx, sz.recommended_size_usd, cur)}\n"
            f"{t('details.max_recommended', lang)}: "
            f"{await format_money(fx, sz.max_recommended_position_usd, cur)}\n"
            f"{t('details.max_capital', lang)}: "
            f"{await format_money(fx, sz.max_capital_utilization_usd, cur)}\n"
            f"{t('details.profit_decay', lang)}: {decay}"
        )

    # ── Risk (§10.5) ──
    parts.append(
        f"<b>{t('details.risk_title', lang)}</b>\n"
        f"{signal.risk_score.emoji} {risk_label(signal.risk_score, lang)} — "
        f"{risk_explanation(signal.risk_score, lang)}")

    # ── Historical Performance & Trade Route (§10.6/§10.7) — Pro gated ──
    if ent.details_advanced:
        parts.append(_historical(reliability, lang))
        parts.append(_trade_route(signal, lang))
    else:
        parts.append(f"{t('details.locked', lang)}\n{t('details.unlock', lang)}")

    return "\n\n".join(parts)


def _spread_line(signal: Signal, lang: str) -> str:
    """Overview spread line. For a funding carry, ``spread_pct`` is the ANNUALIZED rate
    differential (can be tens of %), which is NOT the profit — so it is labelled as such
    and shown alongside the realized net-per-hold, to never be mistaken for the return.
    Price arbs keep the familiar single "Gross Spread"."""
    if signal.arb_type == ArbitrageType.FUNDING:
        return (f"{t('details.funding_annualized', lang)}: {format_pct(signal.spread_pct)} · "
                f"{t('details.net_per_hold', lang)}: {format_pct(signal.net_profit_pct)}")
    return f"{t('details.gross_spread', lang)}: {format_pct(signal.spread_pct)}"


def _historical(reliability: float | None, lang: str) -> str:
    rel = f"{reliability:.0f}%" if reliability is not None else t("details.not_available", lang)
    return (f"<b>{t('details.historical_title', lang)}</b>\n"
            f"{t('details.reliability', lang)}: {rel}\n"
            f"{t('details.spread_frequency', lang)}: ▁▂▃▅▇▆▄")


def _trade_route(signal: Signal, lang: str) -> str:
    title = f"<b>{t('route.title', lang)}</b>"
    if signal.arb_type == ArbitrageType.CEX_CEX:
        return f"{title}\n{t('route.no_transfer', lang)}"
    steps = [t("route.buy", lang, coin=signal.coin, exchange=signal.buy_exchange)]
    if signal.arb_type == ArbitrageType.CROSS_CHAIN and signal.bridge_name:
        mins = (signal.bridge_time_sec or 0) // 60
        steps.append(t("route.bridge", lang, bridge=signal.bridge_name, minutes=mins))
    else:
        network = signal.network or t("route.unknown_network", lang)
        steps.append(t("route.move", lang, coin=signal.coin,
                       exchange=signal.sell_exchange, network=network))
    steps.append(t("route.sell", lang, coin=signal.coin, exchange=signal.sell_exchange))
    return title + "\n" + "\n".join(steps)


async def format_alert(signal: Signal, profile: UserProfile, fx) -> str:
    """Instant Signal Alert message (§13.2)."""
    lang = profile.settings.language.value
    cur = profile.settings.currency.value
    liq = await format_money(fx, signal.liquidity_usd, cur)
    risk_dots = {"Low": "●○○○○", "Medium": "●●●○○", "High": "●●●●●"}[signal.risk_score.value]
    pair = html.escape(signal.trading_pair)
    return "\n".join([
        t("alert.new", lang, pair=pair, profit=format_pct(signal.net_profit_pct)),
        t("alert.route", lang, buy=html.escape(signal.buy_exchange),
          sell=html.escape(signal.sell_exchange)),
        t("alert.meta", lang, liquidity=liq, risk=risk_dots),
    ])
