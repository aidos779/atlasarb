"""Admin Panel handlers (PRD §16) — role-gated, Telegram-native inline UI.

/admin is indistinguishable from an unknown command for non-staff (FR-ADM-01, BR-ADMIN-1):
the role check runs before any admin data is touched. Mutating actions require a reason
and are audited (R-ADMIN-2). Large broadcasts need a second admin (R-ADMIN-3).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.keyboards.inline import back_home
from src.bot.keyboards.screens import admin_menu
from src.bot.states.states import AdminStates
from src.domain.enums import SubscriptionTier, UserRole
from src.domain.user import UserProfile
from src.i18n import t, tier_label

router = Router(name="admin")


def _is_staff(profile: UserProfile) -> bool:
    return profile.role in (UserRole.ADMIN, UserRole.SUPPORT)


@router.message(Command("admin"))
async def cmd_admin(message: Message, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    if not _is_staff(profile):  # BR-ADMIN-1 — reveal nothing
        await message.answer(t("error.unknown_command", lang))
        return
    await message.answer(t("admin.title", lang), reply_markup=admin_menu(lang))


@router.callback_query(F.data == "admin:menu")
async def admin_home(cb: CallbackQuery, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    await cb.message.edit_text(t("admin.title", profile.settings.language.value),
                               reply_markup=admin_menu(profile.settings.language.value))


@router.callback_query(F.data == "admin:monitoring")
async def monitoring(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    data = ctx.admin.monitoring()
    status_lines = "\n".join(
        f"{v}: {s}" for v, s in data["exchange_status"].items())
    m = data["metrics"]
    lang = profile.settings.language.value
    b = InlineKeyboardBuilder()
    for venue in data["exchange_status"]:
        b.button(text=t("admin.kill_btn", lang, venue=venue),
                 callback_data=f"admin:kill:{venue}")
    b.adjust(2)
    b.row(*back_home(lang, back="admin:menu").inline_keyboard[0])
    text = "\n".join([
        t("admin.monitoring_title", lang), "",
        f"<b>{t('admin.connectors', lang)}</b>", status_lines, "",
        t("admin.active_signals", lang, count=data["active_signals"]),
        t("admin.signal_counts", lang, created=m["signals_created"],
          updated=m["signals_updated"], expired=m["signals_expired"]),
        t("admin.latency", lang, detection=m["detection_p95_ms"],
          generation=m["generation_p95_ms"]),
        t("admin.cache_line", lang, size=m["cache_size"], outliers=m["outliers"]),
        t("admin.top_rejections", lang,
          rejections=dict(list(m["rejections"].items())[:5])),
    ])
    await cb.message.edit_text(text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:kill:"))
async def kill_switch(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if profile.role != UserRole.ADMIN:
        await ack(cb, t("admin.admin_only", profile.settings.language.value),
                  show_alert=True)
        return
    venue = cb.data.split(":")[-1]
    ctx.admin.kill_switch(venue, True)
    await ack(cb, t("admin.killed", profile.settings.language.value, venue=venue),
              show_alert=True)


@router.callback_query(F.data == "admin:analytics")
async def analytics(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    mrr = await ctx.admin.mrr()
    metrics = ctx.analytics.engine_metrics()
    lang = profile.settings.language.value
    text = "\n".join([
        t("admin.analytics_title", lang), "",
        t("admin.mrr", lang, amount=f"{mrr:.2f}"),
        t("admin.signals_created", lang, count=metrics["signals_created"]),
        t("admin.signals_by_type", lang, breakdown=metrics["signals_by_type"]),
        t("admin.rejections", lang, rejections=metrics["rejections"]),
    ])
    await cb.message.edit_text(text, reply_markup=back_home(lang, back="admin:menu"))


@router.callback_query(F.data == "admin:logs")
async def logs(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if profile.role != UserRole.ADMIN:  # logs are Admin-only, not Support (§16.7)
        await ack(cb, t("error.unknown_command", profile.settings.language.value),
                  show_alert=True)
        return
    await ack(cb)
    lang = profile.settings.language.value
    m = ctx.analytics.engine_metrics()
    text = "\n".join([
        t("admin.logs_title", lang), "",
        t("admin.api_failures", lang, failures=m.get("api_failures", {})),
        t("admin.rejected_prices", lang, count=m.get("rejected_prices", 0)),
        t("admin.outliers", lang, count=m.get("outliers", 0)),
    ])
    await cb.message.edit_text(text, reply_markup=back_home(lang, back="admin:menu"))


@router.callback_query(F.data == "admin:support")
async def support_queue(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    lang = profile.settings.language.value
    tickets = await ctx.admin.open_tickets()
    b = InlineKeyboardBuilder()
    lines = [t("admin.support_title", lang), ""]
    for ticket in tickets[:10]:
        lines.append(t("admin.ticket_line", lang, id=ticket.id, status=ticket.status,
                       user_id=ticket.user_id, subject=ticket.subject))
        b.button(text=t("admin.ticket_reply_btn", lang, id=ticket.id),
                 callback_data=f"admin:reply:{ticket.id}")
    if not tickets:
        lines.append(t("admin.no_tickets", lang))
    b.adjust(1)
    b.row(*back_home(lang, back="admin:menu").inline_keyboard[0])
    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:reply:"))
async def reply_start(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    ticket_id = int(cb.data.split(":")[-1])
    await state.set_state(AdminStates.awaiting_reply)
    await state.update_data(ticket_id=ticket_id)
    await cb.message.answer(t("admin.reply_prompt", profile.settings.language.value,
                              id=ticket_id))


@router.message(AdminStates.awaiting_reply, F.text)
async def reply_send(message: Message, ctx: BotContext, profile: UserProfile,
                     state: FSMContext, bot) -> None:
    data = await state.get_data()
    await state.clear()
    ticket_id = data["ticket_id"]
    user_id = await ctx.admin.reply_ticket(ticket_id, profile.telegram_user_id,
                                            message.text.strip())
    lang = profile.settings.language.value
    if user_id:
        # The recipient reads their own language, not the replying admin's.
        target = await ctx.users.get(user_id)
        target_lang = (target.settings.language.value if target else lang)
        try:
            await bot.send_message(user_id, t("support.reply_prefix", target_lang,
                                              message=message.text.strip()))
        except Exception:  # noqa: BLE001
            pass
        await message.answer(t("admin.replied", lang, id=ticket_id))
    else:
        await message.answer(t("admin.ticket_not_found", lang))


@router.callback_query(F.data == "admin:users")
async def users_prompt(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    await state.set_state(AdminStates.awaiting_user_lookup)
    await cb.message.answer(t("admin.lookup_prompt", profile.settings.language.value))


@router.message(AdminStates.awaiting_user_lookup, F.text)
async def users_lookup(message: Message, ctx: BotContext, profile: UserProfile,
                       state: FSMContext) -> None:
    await state.clear()
    if not _is_staff(profile):
        return
    lang = profile.settings.language.value
    target = await ctx.admin.lookup(message.text.strip())
    if target is None:
        await message.answer(t("admin.user_not_found", lang))
        return
    b = InlineKeyboardBuilder()
    action = "reactivate" if target.suspended else "suspend"
    b.button(text=t("admin.reactivate_btn" if target.suspended else "admin.suspend_btn", lang),
             callback_data=f"admin:usr:{action}:{target.telegram_user_id}")
    b.button(text=t("admin.set_pro_btn", lang),
             callback_data=f"admin:usr:pro:{target.telegram_user_id}")
    b.button(text=t("admin.reset_filters_btn", lang),
             callback_data=f"admin:usr:resetf:{target.telegram_user_id}")
    b.adjust(1)
    yes_no = (t("admin.yes", lang), t("admin.no", lang))
    text = "\n".join([
        t("admin.user_title", lang, user_id=target.telegram_user_id),
        t("admin.user_role", lang, username=target.username or "—", role=target.role.value),
        t("admin.user_tier", lang, tier=tier_label(target.effective_tier, lang),
          suspended=yes_no[0] if target.suspended else yes_no[1]),
        t("admin.user_onboarded", lang,
          onboarded=yes_no[0] if target.onboarding_complete else yes_no[1]),
    ])
    await message.answer(text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:usr:"))
async def user_action(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    _, _, action, target_id = cb.data.split(":")
    await state.set_state(AdminStates.awaiting_reason)
    await state.update_data(action=action, target=int(target_id))
    await cb.message.answer(t("admin.reason_prompt", profile.settings.language.value))


@router.message(AdminStates.awaiting_reason, F.text)
async def apply_action(message: Message, ctx: BotContext, profile: UserProfile,
                       state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    action, target = data["action"], data["target"]
    reason = message.text.strip()
    admin_id = profile.telegram_user_id
    ok = False
    if action in ("suspend", "reactivate"):
        ok = await ctx.admin.set_suspended(admin_id, target, action == "suspend", reason)
    elif action == "pro":
        ok = await ctx.admin.override_tier(admin_id, target, SubscriptionTier.PRO, reason)
    elif action == "resetf":
        ok = await ctx.admin.reset_filters(admin_id, target, reason)
    lang = profile.settings.language.value
    await message.answer(t("admin.action_done" if ok else "admin.action_failed", lang))


@router.callback_query(F.data == "admin:subs")
async def subs(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    lang = profile.settings.language.value
    mrr = await ctx.admin.mrr()
    await cb.message.edit_text(
        "\n".join([t("admin.subs_title", lang), "",
                   t("admin.subs_mrr", lang, amount=f"{mrr:.2f}"),
                   t("admin.subs_hint", lang)]),
        reply_markup=back_home(lang, back="admin:menu"))


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    await ack(cb)
    await state.set_state(AdminStates.awaiting_broadcast)
    await state.update_data(target_type="all", target_value=None)
    lang = profile.settings.language.value
    note = (t("admin.broadcast_support_note", lang)
            if profile.role == UserRole.SUPPORT else "")
    await cb.message.answer(t("admin.broadcast_prompt", lang, note=note))


@router.message(AdminStates.awaiting_broadcast, F.text)
async def broadcast_preview(message: Message, ctx: BotContext, profile: UserProfile,
                            state: FSMContext) -> None:
    await state.clear()
    is_support = profile.role == UserRole.SUPPORT
    creation = await ctx.admin.create_broadcast(
        profile.telegram_user_id, "all", None, message.text.strip(),
        is_template=is_support, is_support=is_support)
    lang = profile.settings.language.value
    b = InlineKeyboardBuilder()
    b.button(text=t("admin.broadcast_confirm_btn", lang),
             callback_data=f"admin:bcast_confirm:{creation.job_id}")
    b.button(text=t("admin.broadcast_cancel_btn", lang), callback_data="admin:menu")
    b.adjust(1)
    warn = t("admin.broadcast_warning", lang) if creation.needs_second_admin else ""
    preview = t("admin.broadcast_preview", lang, recipients=creation.recipients, warning=warn)
    await message.answer(f"{preview}\n\n{message.text.strip()}", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:bcast_confirm:"))
async def broadcast_confirm(cb: CallbackQuery, ctx: BotContext, profile: UserProfile,
                            bot) -> None:
    if not _is_staff(profile):
        await ack(cb)
        return
    job_id = int(cb.data.split(":")[-1])
    ready = await ctx.admin.confirm_broadcast(job_id, profile.telegram_user_id)
    lang = profile.settings.language.value
    if not ready:
        await ack(cb, t("admin.broadcast_second_admin", lang), show_alert=True)
        return
    # Ack before the fan-out: sending to N users takes far longer than the answer window.
    await ack(cb)
    recipients = await ctx.admin.recipients_for(job_id)
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, cb.message.text.split("\n\n", 1)[-1])
            sent += 1
        except Exception:  # noqa: BLE001
            continue
    await cb.message.edit_text(t("admin.broadcast_sent", lang, count=sent))
