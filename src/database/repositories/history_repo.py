"""History repository — global SignalHistory (§12.5) + per-user interaction lists (§17.4).

Each per-user list caps at 50 entries (BR-HIST-2); older entries are pruned on insert.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import SignalHistory, SignalInteraction
from src.domain.signal import Signal

_MAX_PER_LIST = 50


class HistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def archive_signal(self, signal: Signal) -> None:
        existing = await self._session.get(SignalHistory, signal.id)
        if existing is not None:
            return
        self._session.add(SignalHistory(
            id=signal.id, arb_type=signal.arb_type.value, coin=signal.coin,
            trading_pair=signal.trading_pair, network=signal.network,
            buy_exchange=signal.buy_exchange, sell_exchange=signal.sell_exchange,
            buy_price=float(signal.buy_price), sell_price=float(signal.sell_price),
            spread_pct=float(signal.spread_pct), net_profit_pct=float(signal.net_profit_pct),
            net_profit_usd=float(signal.net_profit_usd),
            liquidity_usd=float(signal.liquidity_usd), risk_score=signal.risk_score.value,
            confidence_score=signal.confidence_score, ranking=signal.ranking.value,
            recommended_size_usd=float(signal.recommended_trade_size_usd),
            snapshot=signal.input_snapshot,
            detected_at=datetime.fromtimestamp(signal.timestamp, tz=UTC),
            expired_at=(datetime.fromtimestamp(signal.expired_at, tz=UTC)
                        if signal.expired_at else None),
            expiry_reason=signal.expiry_reason,
        ))

    async def record_interaction(self, user_id: int, signal: Signal, kind: str) -> None:
        self._session.add(SignalInteraction(
            user_id=user_id, signal_id=signal.id, kind=kind, coin=signal.coin,
            trading_pair=signal.trading_pair, buy_exchange=signal.buy_exchange,
            sell_exchange=signal.sell_exchange, net_profit_pct=float(signal.net_profit_pct),
        ))
        await self._session.flush()
        await self._prune(user_id, kind)

    async def _prune(self, user_id: int, kind: str) -> None:
        stmt = select(SignalInteraction.id).where(
            SignalInteraction.user_id == user_id, SignalInteraction.kind == kind
        ).order_by(SignalInteraction.created_at.desc()).offset(_MAX_PER_LIST)
        stale = [r for r in (await self._session.execute(stmt)).scalars()]
        if stale:
            await self._session.execute(
                delete(SignalInteraction).where(SignalInteraction.id.in_(stale)))

    async def list_interactions(self, user_id: int, kind: str,
                                limit: int = _MAX_PER_LIST) -> list[SignalInteraction]:
        stmt = select(SignalInteraction).where(
            SignalInteraction.user_id == user_id, SignalInteraction.kind == kind
        ).order_by(SignalInteraction.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def type_reliability(self, arb_type: str, buy: str, sell: str) -> float:
        """Rolling hit-rate proxy for the confidence factor (§11.5). 0..100."""
        stmt = select(SignalHistory.expiry_reason).where(
            SignalHistory.arb_type == arb_type,
            SignalHistory.buy_exchange == buy, SignalHistory.sell_exchange == sell,
        ).order_by(SignalHistory.expired_at.desc()).limit(50)
        reasons = [r for r in (await self._session.execute(stmt)).scalars()]
        if not reasons:
            return 60.0
        held = sum(1 for r in reasons if r in ("TTL_EXCEEDED", "FUNDING_SETTLED"))
        return round(held / len(reasons) * 100, 1)
