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

from src.bot.context import BotContext
from src.bot.i18n import t
from src.bot.keyboards.inline import back_home
from src.bot.keyboards.screens import admin_menu
from src.bot.states.states import AdminStates
from src.domain.enums import SubscriptionTier, UserRole
from src.domain.user import UserProfile

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
        await cb.answer()
        return
    await cb.message.edit_text(t("admin.title", profile.settings.language.value),
                               reply_markup=admin_menu(profile.settings.language.value))
    await cb.answer()


@router.callback_query(F.data == "admin:monitoring")
async def monitoring(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    data = ctx.admin.monitoring()
    status_lines = "\n".join(
        f"{v}: {s}" for v, s in data["exchange_status"].items())
    m = data["metrics"]
    b = InlineKeyboardBuilder()
    for venue in data["exchange_status"]:
        b.button(text=f"⛔ Kill {venue}", callback_data=f"admin:kill:{venue}")
    b.adjust(2)
    b.row(*back_home(profile.settings.language.value, back="admin:menu").inline_keyboard[0])
    text = (
        "📡 <b>Signal Monitoring</b>\n\n"
        f"<b>Connectors</b>\n{status_lines}\n\n"
        f"Active signals: {data['active_signals']}\n"
        f"Created: {m['signals_created']} · Updated: {m['signals_updated']} · "
        f"Expired: {m['signals_expired']}\n"
        f"Detection p95: {m['detection_p95_ms']}ms · Gen p95: {m['generation_p95_ms']}ms\n"
        f"Cache: {m['cache_size']} · Outliers: {m['outliers']}\n"
        f"Top rejections: {dict(list(m['rejections'].items())[:5])}"
    )
    await cb.message.edit_text(text, reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("admin:kill:"))
async def kill_switch(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if profile.role != UserRole.ADMIN:
        await cb.answer("Admin only", show_alert=True)
        return
    venue = cb.data.split(":")[-1]
    ctx.admin.kill_switch(venue, True)
    await cb.answer(f"⛔ {venue} disabled", show_alert=True)


@router.callback_query(F.data == "admin:analytics")
async def analytics(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    mrr = await ctx.admin.mrr()
    metrics = ctx.analytics.engine_metrics()
    text = (
        "📊 <b>Platform Analytics</b>\n\n"
        f"MRR (cumulative billed): ${mrr:.2f}\n"
        f"Signals created: {metrics['signals_created']}\n"
        f"By type: {metrics['signals_by_type']}\n"
        f"Rejections: {metrics['rejections']}"
    )
    await cb.message.edit_text(text, reply_markup=back_home(
        profile.settings.language.value, back="admin:menu"))
    await cb.answer()


@router.callback_query(F.data == "admin:logs")
async def logs(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if profile.role != UserRole.ADMIN:  # logs are Admin-only, not Support (§16.7)
        await cb.answer(t("error.unknown_command", profile.settings.language.value),
                        show_alert=True)
        return
    m = ctx.analytics.engine_metrics()
    text = ("📜 <b>System Logs (summary)</b>\n\n"
            f"API failures: {m.get('api_failures', {})}\n"
            f"Rejected prices: {m.get('rejected_prices', 0)}\n"
            f"Outliers: {m.get('outliers', 0)}")
    await cb.message.edit_text(text, reply_markup=back_home(
        profile.settings.language.value, back="admin:menu"))
    await cb.answer()


@router.callback_query(F.data == "admin:support")
async def support_queue(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    tickets = await ctx.admin.open_tickets()
    b = InlineKeyboardBuilder()
    lines = ["🆘 <b>Support Queue</b>", ""]
    for ticket in tickets[:10]:
        lines.append(f"#{ticket.id} [{ticket.status}] user {ticket.user_id}: {ticket.subject}")
        b.button(text=f"Reply #{ticket.id}", callback_data=f"admin:reply:{ticket.id}")
    if not tickets:
        lines.append("No open tickets.")
    b.adjust(1)
    b.row(*back_home(profile.settings.language.value, back="admin:menu").inline_keyboard[0])
    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("admin:reply:"))
async def reply_start(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    ticket_id = int(cb.data.split(":")[-1])
    await state.set_state(AdminStates.awaiting_reply)
    await state.update_data(ticket_id=ticket_id)
    await cb.message.answer(f"Type your reply to ticket #{ticket_id} (or /cancel):")
    await cb.answer()


@router.message(AdminStates.awaiting_reply, F.text)
async def reply_send(message: Message, ctx: BotContext, profile: UserProfile,
                     state: FSMContext, bot) -> None:
    data = await state.get_data()
    await state.clear()
    ticket_id = data["ticket_id"]
    user_id = await ctx.admin.reply_ticket(ticket_id, profile.telegram_user_id,
                                            message.text.strip())
    if user_id:
        try:
            await bot.send_message(
                user_id, f"🆘 <b>Support</b>: {message.text.strip()}")
        except Exception:  # noqa: BLE001
            pass
        await message.answer(f"✅ Replied to ticket #{ticket_id}.")
    else:
        await message.answer("Ticket not found.")


@router.callback_query(F.data == "admin:users")
async def users_prompt(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    await state.set_state(AdminStates.awaiting_user_lookup)
    await cb.message.answer("Send a Telegram user_id or @username to look up (or /cancel):")
    await cb.answer()


@router.message(AdminStates.awaiting_user_lookup, F.text)
async def users_lookup(message: Message, ctx: BotContext, profile: UserProfile,
                       state: FSMContext) -> None:
    await state.clear()
    if not _is_staff(profile):
        return
    target = await ctx.admin.lookup(message.text.strip())
    if target is None:
        await message.answer("User not found.")
        return
    b = InlineKeyboardBuilder()
    action = "reactivate" if target.suspended else "suspend"
    b.button(text=f"{'✅ Reactivate' if target.suspended else '⛔ Suspend'}",
             callback_data=f"admin:usr:{action}:{target.telegram_user_id}")
    b.button(text="⬆️ Set Pro", callback_data=f"admin:usr:pro:{target.telegram_user_id}")
    b.button(text="♻️ Reset Filters", callback_data=f"admin:usr:resetf:{target.telegram_user_id}")
    b.adjust(1)
    text = (
        f"👤 <b>User {target.telegram_user_id}</b>\n"
        f"@{target.username or '—'} · role {target.role.value}\n"
        f"Tier: {target.effective_tier.value} · Suspended: {target.suspended}\n"
        f"Onboarded: {target.onboarding_complete}"
    )
    await message.answer(text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:usr:"))
async def user_action(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    _, _, action, target_id = cb.data.split(":")
    await state.set_state(AdminStates.awaiting_reason)
    await state.update_data(action=action, target=int(target_id))
    await cb.message.answer("Enter a reason for this action (required, audited):")
    await cb.answer()


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
    await message.answer("✅ Done (audited)." if ok else "Failed — user not found.")


@router.callback_query(F.data == "admin:subs")
async def subs(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    mrr = await ctx.admin.mrr()
    await cb.message.edit_text(
        f"💳 <b>Subscription Management</b>\n\nMRR (billed): ${mrr:.2f}\n"
        "Look up a user via 👥 User Management to grant/comp/downgrade.",
        reply_markup=back_home(profile.settings.language.value, back="admin:menu"))
    await cb.answer()


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    await state.set_state(AdminStates.awaiting_broadcast)
    await state.update_data(target_type="all", target_value=None)
    note = ("Support role: only pre-approved templates are sent."
            if profile.role == UserRole.SUPPORT else "")
    await cb.message.answer(f"Type the broadcast message (targets: All Users). {note}\n(/cancel)")
    await cb.answer()


@router.message(AdminStates.awaiting_broadcast, F.text)
async def broadcast_preview(message: Message, ctx: BotContext, profile: UserProfile,
                            state: FSMContext) -> None:
    await state.clear()
    is_support = profile.role == UserRole.SUPPORT
    creation = await ctx.admin.create_broadcast(
        profile.telegram_user_id, "all", None, message.text.strip(),
        is_template=is_support, is_support=is_support)
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm & Send", callback_data=f"admin:bcast_confirm:{creation.job_id}")
    b.button(text="❌ Cancel", callback_data="admin:menu")
    b.adjust(1)
    warn = ("\n⚠️ Large audience — a second Administrator must confirm (two-person rule)."
            if creation.needs_second_admin else "")
    await message.answer(
        f"📢 <b>Preview</b> → {creation.recipients} recipients{warn}\n\n{message.text.strip()}",
        reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:bcast_confirm:"))
async def broadcast_confirm(cb: CallbackQuery, ctx: BotContext, profile: UserProfile,
                            bot) -> None:
    if not _is_staff(profile):
        await cb.answer()
        return
    job_id = int(cb.data.split(":")[-1])
    ready = await ctx.admin.confirm_broadcast(job_id, profile.telegram_user_id)
    if not ready:
        await cb.answer("Recorded. Awaiting a second Administrator's confirmation.",
                        show_alert=True)
        return
    recipients = await ctx.admin.recipients_for(job_id)
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, cb.message.text.split("\n\n", 1)[-1])
            sent += 1
        except Exception:  # noqa: BLE001
            continue
    await cb.message.edit_text(f"✅ Broadcast sent to {sent} users.")
    await cb.answer()
