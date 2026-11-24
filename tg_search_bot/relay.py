"""双向消息：所有机器人的用户消息都由母机器人转给指定接收人；
接收人「回复」那条消息，就通过用户当时聊天的那个机器人回给用户。"""
from __future__ import annotations

import html
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from .content import message_summary, transfer_message

ACK_TEXT = "✅ 消息已送达，回复会直接发到这里。"
ACK_INTERVAL_SECONDS = 600
REPLIED_TEXT = "✅ 已回复"
REPLIED_CONFIRM_SECONDS = 10


def relay_receiver(hub) -> int:
    if hub.store.get_setting("relay_enabled", "1") != "1":
        return 0
    raw = hub.store.get_setting("relay_receiver", "")
    return int(raw) if raw.lstrip("-").isdigit() else 0


def relay_delete_delay(hub) -> int:
    if hub.store.get_setting("relay_autodelete", "1") != "1":
        return 0
    return int(hub.config.relay_delete_seconds)


async def forward_to_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """用户 → 接收人（经母机器人）。返回 False 表示没开双向/没设置接收人。"""
    hub = context.application.bot_data["hub"]
    bot_id = int(context.application.bot_data["bot_id"])
    message = update.effective_message
    user = update.effective_user
    receiver = relay_receiver(hub)
    mother = hub.mother_bot()
    if not receiver or mother is None or not message or not user:
        return False
    if int(user.id) == receiver:
        return False  # 接收人自己发的普通消息不再转给自己
    quote = None
    replied = getattr(message, "reply_to_message", None)
    if replied is not None:
        row = hub.store.relay(bot_id, message.chat_id, replied.message_id)
        if row is not None and str(row["side"]) == "user":
            quote = int(row["peer_message_id"] or 0) or None
    bot_name = context.bot.username or str(bot_id)
    name = " ".join(x for x in (user.first_name, user.last_name) if x) or "用户"
    header = (
        f"💬 来自 @{html.escape(bot_name)} 的消息\n"
        f"用户：{html.escape(name)}"
        + (f" @{html.escape(user.username)}" if user.username else "")
        + f"  ID <code>{user.id}</code>\n"
        "↩️ 直接回复下面那条消息即可回复对方"
    )
    delay = relay_delete_delay(hub)
    mother_id = hub.mother_bot_id
    try:
        from telegram import ReplyParameters

        head = await mother.send_message(
            receiver, header, parse_mode=ParseMode.HTML,
            reply_parameters=(ReplyParameters(message_id=quote, allow_sending_without_reply=True)
                              if quote else None),
        )
        sent = await transfer_message(
            context.bot, bot_id, message, mother, mother_id, receiver, reply_to=head.message_id,
        )
    except TelegramError as exc:
        logging.warning("relay to receiver %s failed: %s", receiver, exc)
        await message.reply_text("暂时无法联系客服，请稍后再试。")
        return True
    for item in (head, sent):
        if item is None:
            continue
        hub.store.map_relay("receiver", mother_id, receiver, item.message_id, bot_id, user.id, message.message_id)
        hub.store.schedule_deletion(mother_id, receiver, item.message_id, delay)
    last = context.user_data.get("relay_ack_at", 0)
    if time.time() - float(last or 0) > ACK_INTERVAL_SECONDS:
        context.user_data["relay_ack_at"] = time.time()
        await message.reply_text(ACK_TEXT)
    return True


async def deliver_receiver_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, row) -> None:
    """接收人回复 → 用户（通过用户当时聊天的那个机器人）。"""
    hub = context.application.bot_data["hub"]
    this_id = int(context.application.bot_data["bot_id"])
    message = update.effective_message
    target_id = int(row["bot_id"])
    user_id = int(row["user_id"])
    target_bot = hub.bot_for(target_id)
    if target_bot is None:
        await message.reply_text("❌ 发送失败：该机器人已停用或已删除。")
        return
    try:
        sent = await transfer_message(
            context.bot, this_id, message, target_bot, target_id, user_id,
            reply_to=int(row["peer_message_id"] or 0) or None,
        )
    except Forbidden:
        await message.reply_text("❌ 发送失败：用户已屏蔽机器人或已删除对话。")
        return
    except TelegramError as exc:
        reason = str(getattr(exc, "message", "") or exc)
        if "chat not found" in reason.lower():
            reason = "用户不存在或从未私聊过该机器人"
        await message.reply_text(f"❌ 发送失败：{reason}")
        return
    if sent is not None:
        hub.store.map_relay("user", target_id, user_id, sent.message_id, target_id, user_id, message.message_id)
        hub.store.schedule_deletion(target_id, user_id, sent.message_id, relay_delete_delay(hub))
    confirm = await message.reply_text(REPLIED_TEXT)
    if confirm is not None:
        hub.store.schedule_deletion(this_id, confirm.chat_id, confirm.message_id, REPLIED_CONFIRM_SECONDS)
    logging.info("relay reply %s -> bot %s user %s: %s", this_id, target_id, user_id,
                 message_summary(message)[:40])


def find_receiver_row(hub, bot_id: int, message):
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return None
    row = hub.store.relay(bot_id, message.chat_id, replied.message_id)
    if row is not None and str(row["side"]) == "receiver":
        return row
    return None
