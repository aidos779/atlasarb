"""Signal rendering — compact card (§9.3) and full Details (§10).

Respects the user's Currency and Timezone (BR-DETAILS-3) and tier gating: Free sees
Overview/Profit/Fees/Liquidity; Historical Performance & Trade Route are Pro-only,
rendered as a locked preview for Free (BR-DETAILS-2).
"""
from __future__ import annotations

import html

from src.bot.formatters.money import format_datetime, format_money, format_pct
from src.bot.i18n import t
from src.domain.entitlements import entitlements_for
from src.domain.enums import ArbitrageType, SignalStatus
from src.domain.signal import Signal
from src.domain.user import UserProfile

_CONF_THRESHOLD = 70


async def format_card(signal: Signal, profile: UserProfile, fx,
                      confidence_threshold: float = _CONF_THRESHOLD) -> str:
    lang = profile.settings.language.value
    cur = profile.settings.currency.value
    net_usd = await format_money(fx, signal.net_profit_usd, cur)
    rank = signal.ranking.emoji
    pair = html.escape(signal.trading_pair)
    network = signal.network or "—"
    liq = await format_money(fx, signal.liquidity_usd, cur)
    lines = [
        f"{rank} <b>{pair}</b>  {format_pct(signal.net_profit_pct)} (~{net_usd})",
        f"Buy: {html.escape(signal.buy_exchange)} → Sell: {html.escape(signal.sell_exchange)}",
        f"Liquidity: {liq} · Network: {network} · Risk: {signal.risk_score.emoji} "
        f"{signal.risk_score.value}",
        f"Active {signal.age_sec()}s",
    ]
    if signal.is_low_confidence(confidence_threshold):
        lines.append("⚠️ Low confidence")
    _ = lang
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
    parts.append(
        f"<b>{pair}</b> — {signal.arb_type.label} {signal.ranking.emoji}\n"
        f"Signal ID: <code>{signal.id[:8]}</code> · Status: {status_emoji} {signal.status.value}\n"
        f"Detected: {format_datetime(signal.timestamp, tz)} · Active {signal.age_sec()}s\n\n"
        f"Buy on {html.escape(signal.buy_exchange)} @ {buy_price}\n"
        f"Sell on {html.escape(signal.sell_exchange)} @ {sell_price}\n\n"
        f"Gross Spread: {format_pct(signal.spread_pct)} · Confidence: {signal.confidence_score}%"
    )

    # ── Profit Breakdown (§10.2) ──
    bd = signal.profit_breakdown
    if bd is not None:
        net_usd = await format_money(fx, bd.net_profit_usd, cur)
        parts.append(
            "<b>Profit Breakdown</b>\n"
            f"Gross Spread: {format_pct(bd.gross_spread_pct)}\n"
            f"Trading fees: −{await format_money(fx, bd.trading_fees_usd, cur)}\n"
            f"Withdrawal/network: −{await format_money(fx, bd.withdrawal_fees_usd + bd.gas_fees_usd + bd.bridge_fees_usd, cur)}\n"
            f"Est. slippage: −{await format_money(fx, bd.slippage_cost_usd, cur)}\n"
            f"<b>Net Profit: {format_pct(bd.net_profit_pct)} (~{net_usd})</b>"
        )

    # ── Fees (§10.3) ──
    parts.append(
        "<b>Fees</b>\n"
        f"Trading (taker, both legs): {await format_money(fx, signal.trading_fees, cur)}\n"
        f"Withdrawal: {await format_money(fx, signal.withdrawal_fees, cur)}\n"
        f"Network/gas: {await format_money(fx, signal.network_fees, cur)}"
    )

    # ── Liquidity + sizing (§10.4 / §8.12) ──
    if signal.sizing is not None:
        sz = signal.sizing
        decay = " · ".join(f"{int(p)}%→{r:.2f}%" for p, r in sz.profit_decay)
        parts.append(
            "<b>Liquidity</b>\n"
            f"Recommended size: {await format_money(fx, sz.recommended_size_usd, cur)}\n"
            f"Max recommended: {await format_money(fx, sz.max_recommended_position_usd, cur)}\n"
            f"Max capital util.: {await format_money(fx, sz.max_capital_utilization_usd, cur)}\n"
            f"Profit decay: {decay}"
        )

    # ── Risk (§10.5) ──
    parts.append(f"<b>Risk</b>\n{signal.risk_score.emoji} {signal.risk_score.value} — "
                 f"{_risk_explanation(signal)}")

    # ── Historical Performance & Trade Route (§10.6/§10.7) — Pro gated ──
    if ent.details_advanced:
        parts.append(_historical(signal, reliability))
        parts.append(_trade_route(signal))
    else:
        parts.append(f"{t('details.locked', lang)}\n{t('details.unlock', lang)}")

    return "\n\n".join(parts)


def _risk_explanation(signal: Signal) -> str:
    if signal.risk_score.value == "Low":
        return "deep liquidity on both sides, no network transfer required."
    if signal.risk_score.value == "Medium":
        return "some liquidity thinness, transfer required, or elevated volatility."
    return "thin liquidity, cross-chain bridge, or high gas volatility."


def _historical(signal: Signal, reliability: float | None) -> str:
    rel = f"{reliability:.0f}%" if reliability is not None else "n/a"
    return ("<b>Historical Performance</b>\n"
            f"Venue-pair reliability (rolling): {rel}\n"
            "Spread frequency: ▁▂▃▅▇▆▄")


def _trade_route(signal: Signal) -> str:
    if signal.arb_type == ArbitrageType.CEX_CEX:
        return ("<b>Trade Route</b>\n"
                "No transfer required — both legs can be executed if you already hold "
                "balances on both exchanges. (Informational only — the bot does not execute.)")
    steps = [f"1. Buy {signal.coin} on {signal.buy_exchange}"]
    if signal.arb_type == ArbitrageType.CROSS_CHAIN and signal.bridge_name:
        mins = (signal.bridge_time_sec or 0) // 60
        steps.append(f"2. Bridge via {signal.bridge_name} (~{mins} min)")
        steps.append(f"3. Sell {signal.coin} on {signal.sell_exchange}")
    else:
        network = signal.network or "chain"
        steps.append(f"2. Move {signal.coin} → {signal.sell_exchange} (network: {network})")
        steps.append(f"3. Sell {signal.coin} on {signal.sell_exchange}")
    return "<b>Trade Route</b>\n" + "\n".join(steps)


async def format_alert(signal: Signal, profile: UserProfile, fx) -> str:
    """Instant Signal Alert message (§13.2)."""
    cur = profile.settings.currency.value
    liq = await format_money(fx, signal.liquidity_usd, cur)
    risk_dots = {"Low": "●○○○○", "Medium": "●●●○○", "High": "●●●●●"}[signal.risk_score.value]
    pair = html.escape(signal.trading_pair)
    return (
        f"🚨 <b>New Signal: {pair}</b>  {format_pct(signal.net_profit_pct)}\n"
        f"Buy {html.escape(signal.buy_exchange)} → Sell {html.escape(signal.sell_exchange)}\n"
        f"Liquidity {liq} · Risk {risk_dots}"
    )
