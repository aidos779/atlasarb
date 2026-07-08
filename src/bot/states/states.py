"""FSM state groups for multi-step text input (PRD §7.2 /cancel governs all)."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SearchStates(StatesGroup):
    awaiting_query = State()


class FilterStates(StatesGroup):
    awaiting_value = State()   # data: {"field": ...}


class SupportStates(StatesGroup):
    awaiting_message = State()


class AdminStates(StatesGroup):
    awaiting_user_lookup = State()
    awaiting_reason = State()      # data: {"action", "target"}
    awaiting_broadcast = State()   # data: {"target_type", "target_value"}
    awaiting_reply = State()       # data: {"ticket_id", "user_id"}
