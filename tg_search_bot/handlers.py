"""所有机器人共用的处理逻辑；母机器人多出 克隆 / 子机器人列表 / 开发者面板。"""
from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import relay
from .content import capture_portable, content_preview, send_portable
from .search import (
    build_submission, deliver_entry, entry_card, keyword_auto_reply,
    parse_rich_submission_command, review_keyboard, search_reply,
)
from .storage import db_size_bytes

MENU_SEARCH = "🔎 搜索"
MENU_SUBMIT = "📝 提交收录"
MENU_MY = "📋 我的提交"
MENU_HELP = "📖 帮助"
MENU_CLONE = "🤖 克隆机器人"
MENU_MANAGE = "🛠 收录管理"
MENU_DEV = "👑 开发者面板"
MENU_CHILDREN = "📋 子机器人列表"

DEFAULT_WELCOME = "👋 欢迎使用搜索机器人！\n直接发送关键词即可搜索，需要帮助请点「📖 帮助」。"
TOKEN_PATTERN = re.compile(r"^\s*(\d{5,}:[A-Za-z0-9_-]{30,})\s*$")
PAGE_SIZE = 10

SUBMIT_HELP = (
    "📝 提交收录\n\n"
    "• 发送：关键词 搜录 内容（可带图片、视频、文件，格式原样保留）\n"
    "• 或者：回复要收录的消息，发送「关键词 搜录」，整条消息原样收录（含按钮）\n"
    "• 管理员审核后公开；同一关键词以最新一条为准"
)


# ------------------------------------------------------------------ 基础
def hub_of(context):
    return context.application.bot_data["hub"]


def store_of(context):
    return context.application.bot_data["store"]


def is_mother(context) -> bool:
    return bool(context.application.bot_data.get("is_mother"))


def uid_of(update: Update) -> int:
    return int(update.effective_user.id) if update.effective_user else 0


def is_developer(context, user_id: int) -> bool:
    return hub_of(context).is_developer(user_id)


def is_bot_admin(context, user_id: int) -> bool:
    """开发者在所有机器人上都是管理员；子机器人的提交 Token 者是该机器人的超级管理员。"""
    if not user_id:
        return False
    if is_developer(context, user_id):
        return True
    owner = int(context.application.bot_data.get("owner_id") or 0)
    return not is_mother(context) and owner == int(user_id)


def reviewer_ids(context) -> set[int]:
    ids = set(hub_of(context).developer_ids())
    owner = int(context.application.bot_data.get("owner_id") or 0)
    if owner and not is_mother(context):
        ids.add(owner)
    return ids


def main_keyboard(context, user_id: int) -> ReplyKeyboardMarkup:
    rows = [[MENU_SEARCH, MENU_SUBMIT], [MENU_MY, MENU_HELP]]
    if is_mother(context):
        rows.append([MENU_CLONE])
    if is_bot_admin(context, user_id):
        rows.append([MENU_MANAGE])
    if is_mother(context) and is_developer(context, user_id):
        rows.append([MENU_DEV, MENU_CHILDREN])
    return ReplyKeyboardMarkup(
        [[KeyboardButton(x) for x in row] for row in rows], resize_keyboard=True,
    )


def help_text(context, user_id: int) -> str:
    lines = [
        "📖 使用帮助", "",
        "🔎 搜索",
        "• 直接发送关键词即可查看内容，如 XX 或 XX地址",
        "• /search 关键词：模糊查找，最多列出10个",
        "",
        "📝 收录",
        "• 关键词 搜录 内容（可附图片、视频、文件）",
        "• 回复一条消息，发送「关键词 搜录」：原样收录",
        "• 管理员审核后公开；同一关键词以最新一条为准；/my 我的提交",
        "",
        "💬 联系我们",
        "• 直接发消息（文字、图片、视频、文件、语音都可以），回复会发到这里",
    ]
    if is_mother(context):
        lines += [
            "", "🤖 克隆机器人",
            "• 点「🤖 克隆机器人」发送 Bot Token，审核通过后自动启动",
            "• 提交 Token 的人是新机器人的超级管理员（可审核、管理收录）",
            "• 每个机器人的收录数据独立，子机器人存储上限 2GB",
        ]
    if is_bot_admin(context, user_id):
        lines += ["", "🛠 管理员", "• 「🛠 收录管理」：审核、查看、删除收录，查看存储用量"]
    if is_mother(context) and is_developer(context, user_id):
        lines += ["• 「👑 开发者面板」：控制所有机器人、欢迎消息、双向、群发"]
    return "\n".join(lines)


def register(update: Update, context) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user and chat and chat.type == ChatType.PRIVATE:
        store_of(context).touch_user(user.id, user.username or "", user.first_name or "", user.last_name or "")


def fmt_bytes(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} GB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


def storage_text(context) -> str:
    used = store_of(context).storage_usage_bytes()
    quota = int(context.application.bot_data.get("quota") or 0)
    if not quota:
        return f"💾 存储用量：{fmt_bytes(used)}"
    return f"💾 存储用量：{fmt_bytes(used)} / {fmt_bytes(quota)}（{used * 100 / quota:.1f}%）"


def quota_exceeded(context) -> bool:
    quota = int(context.application.bot_data.get("quota") or 0)
    return bool(quota) and store_of(context).storage_usage_bytes() >= quota


# ------------------------------------------------------------------ 命令
async def send_welcome(update: Update, context) -> None:
    hub = hub_of(context)
    message = update.effective_message
    user_id = uid_of(update)
    keyboard = main_keyboard(context, user_id)
    content = hub.get_json("welcome_content")
    if content:
        try:
            if content.get("buttons_json", "[]") not in ("", "[]"):
                await send_portable(context.bot, context.application.bot_data["bot_id"],
                                    message.chat_id, content, hub.store)
                await message.reply_text("👇 请选择功能", reply_markup=keyboard)
            else:
                await send_portable(context.bot, context.application.bot_data["bot_id"],
                                    message.chat_id, content, hub.store, reply_markup=keyboard)
            return
        except (TelegramError, ValueError):
            logging.exception("welcome message failed")
    await message.reply_text(DEFAULT_WELCOME, reply_markup=keyboard)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register(update, context)
    context.user_data.pop("mode", None)
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("请私聊我使用全部功能；群里直接发关键词即可搜索。")
        return
    await send_welcome(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register(update, context)
    await update.effective_message.reply_text(help_text(context, uid_of(update)))


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"你的 ID：<code>{uid_of(update)}</code>", parse_mode=ParseMode.HTML,
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mode", None)
    context.user_data.pop("broadcast", None)
    await update.effective_message.reply_text(
        "已取消。", reply_markup=main_keyboard(context, uid_of(update)) if update.effective_chat
        and update.effective_chat.type == ChatType.PRIVATE else None,
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register(update, context)
    query = " ".join(context.args or []).strip()
    if not query:
        context.user_data["mode"] = "search"
        await update.effective_message.reply_text("请发送要搜索的关键词：")
        return
    await search_reply(update.effective_message, store_of(context), query, uid_of(update))


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register(update, context)
    entries = store_of(context).user_entries(uid_of(update), limit=20)
    if not entries:
        await update.effective_message.reply_text("你还没有提交过收录。\n\n" + SUBMIT_HELP)
        return
    labels = {"pending": "⏳待审核", "approved": "✅已通过", "rejected": "❌已拒绝"}
    lines = ["📋 我的提交（最近20条）", ""]
    for e in entries:
        lines.append(f"#{e.id} {html.escape(e.title)} · {labels.get(e.status, e.status)}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ 收录提交
async def handle_submission(update: Update, context, keyword: str, content: str) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if quota_exceeded(context):
        await message.reply_text("❌ 本机器人存储空间已满，暂时无法新增收录。\n" + storage_text(context))
        return
    params = await build_submission(context.bot, message, chat, keyword, content)
    if params is None:
        await message.reply_text("请在「关键词 搜录」后写上内容；\n或回复要收录的消息，再发送「关键词 搜录」。")
        return
    store = store_of(context)
    auto = is_bot_admin(context, user.id)
    try:
        entry_id = store.add_rich_submission(
            user_id=user.id, username=user.username or "",
            status="approved" if auto else "pending", **params,
        )
    except ValueError as exc:
        await message.reply_text(f"提交失败：{exc}")
        return
    if auto:
        await message.reply_text(f"✅ 已收录 #{entry_id}，关键词：{keyword}（管理员提交直接生效，同关键词旧内容已替换）")
        return
    await message.reply_text(
        f"收录提交成功，编号 #{entry_id}，关键词：{keyword}\n等待管理员审核；同一关键词审核通过后以最新内容为准。"
    )
    entry = store.get(entry_id)
    for admin_id in reviewer_ids(context):
        try:
            await deliver_entry(entry, bot=context.bot, chat_id=admin_id)
            await context.bot.send_message(
                admin_id, "收到新的待审核收录：\n\n" + entry_card(entry),
                parse_mode=ParseMode.HTML, reply_markup=review_keyboard(entry_id),
            )
        except TelegramError:
            logging.info("reviewer %s not reachable", admin_id)


# ------------------------------------------------------------------ 私聊消息
async def private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not update.effective_user:
        return
    register(update, context)
    hub = hub_of(context)
    user_id = uid_of(update)
    bot_id = int(context.application.bot_data["bot_id"])

    # 1. 接收人回复转来的消息 → 回给用户
    row = relay.find_receiver_row(hub, bot_id, message)
    if row is not None:
        await relay.deliver_receiver_reply(update, context, row)
        return

    text = (message.text or "").strip()
    caption = (message.caption or "").strip()

    # 2. 等待输入的操作
    mode = context.user_data.get("mode")
    if mode and await handle_mode(update, context, mode, text):
        return

    # 3. 菜单按钮
    if text and await handle_menu(update, context, text):
        return

    # 4. 克隆：母机器人里直接发 Token 也可以
    if is_mother(context) and text and TOKEN_PATTERN.match(text):
        await submit_clone_token(update, context, text)
        return

    # 5. 收录提交
    parsed = parse_rich_submission_command(text or caption)
    if parsed:
        await handle_submission(update, context, *parsed)
        return

    # 6. 关键词
    if text and await keyword_auto_reply(message, store_of(context), text, user_id):
        return

    # 7. 双向
    if await relay.forward_to_receiver(update, context):
        return
    if text:
        await message.reply_text(
            "没有找到该关键词。发送 /search 关键词 可以模糊查找。",
            reply_markup=main_keyboard(context, user_id),
        )


async def group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not update.effective_user:
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        return
    parsed = parse_rich_submission_command(text)
    if parsed:
        await handle_submission(update, context, *parsed)
        return
    if message.text:
        await keyword_auto_reply(message, store_of(context), text, uid_of(update))


async def handle_menu(update: Update, context, text: str) -> bool:
    user_id = uid_of(update)
    message = update.effective_message
    if text == MENU_SEARCH:
        context.user_data["mode"] = "search"
        await message.reply_text("请发送要搜索的关键词：")
    elif text == MENU_SUBMIT:
        await message.reply_text(SUBMIT_HELP)
    elif text == MENU_MY:
        await my_cmd(update, context)
    elif text == MENU_HELP:
        await message.reply_text(help_text(context, user_id))
    elif text == MENU_CLONE and is_mother(context):
        await clone_menu(update, context)
    elif text == MENU_MANAGE and is_bot_admin(context, user_id):
        body, markup = manage_view(context)
        await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
    elif text == MENU_DEV and is_mother(context) and is_developer(context, user_id):
        body, markup = dev_home_view(context)
        await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
    elif text == MENU_CHILDREN and is_mother(context) and is_developer(context, user_id):
        body, markup = children_view(context, 0)
        await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        return False
    return True


async def handle_mode(update: Update, context, mode: str, text: str) -> bool:
    """处理“请发送…”之后的那条消息。返回 True 表示已处理。"""
    message = update.effective_message
    user_id = uid_of(update)
    hub = hub_of(context)
    if text in {MENU_SEARCH, MENU_SUBMIT, MENU_MY, MENU_HELP, MENU_CLONE, MENU_MANAGE, MENU_DEV, MENU_CHILDREN}:
        context.user_data.pop("mode", None)
        return False
    if mode == "search":
        if not text:
            return False
        context.user_data.pop("mode", None)
        await search_reply(message, store_of(context), text, user_id)
        return True
    if mode == "token":
        context.user_data.pop("mode", None)
        await submit_clone_token(update, context, text)
        return True
    dev_ok = is_mother(context) and is_developer(context, user_id)
    if not dev_ok:
        context.user_data.pop("mode", None)
        return False
    if mode == "dev_welcome":
        context.user_data.pop("mode", None)
        try:
            content = await capture_portable(context.bot, hub.mother_bot_id, message, hub.media_dir(), "welcome")
        except (ValueError, TelegramError) as exc:
            await message.reply_text(f"❌ 设置失败：{exc}")
            return True
        hub.set_json("welcome_content", content)
        synced = await hub.apply_description_all() if hub.store.get_setting(
            "welcome_sync_description", "1") == "1" else 0
        await message.reply_text(
            "✅ 欢迎消息已保存，所有机器人（母机器人和全部子机器人）点开 /start 都会显示这条内容。"
            + (f"\n已同步到 {synced} 个机器人的简介页。" if synced else "")
        )
        await send_portable(context.bot, hub.mother_bot_id, message.chat_id, content, hub.store)
        return True
    if mode == "dev_receiver":
        target = parse_user_target(message, text)
        if not target:
            await message.reply_text("请发送数字 ID，或转发一条该账号的消息。发送 /cancel 取消。")
            return True
        context.user_data.pop("mode", None)
        hub.store.set_setting("relay_receiver", str(target))
        await message.reply_text(
            f"✅ 双向接收人已设为 <code>{target}</code>。\n"
            "注意：该账号必须先私聊母机器人点一次 /start，才能收到消息。",
            parse_mode=ParseMode.HTML,
        )
        return True
    if mode == "dev_adddev":
        target = parse_user_target(message, text)
        if not target:
            await message.reply_text("请发送数字 ID，或转发一条该账号的消息。发送 /cancel 取消。")
            return True
        context.user_data.pop("mode", None)
        hub.store.add_developer(target, user_id)
        await message.reply_text(f"✅ 已添加开发者 <code>{target}</code>。", parse_mode=ParseMode.HTML)
        return True
    if mode.startswith("dev_broadcast:"):
        context.user_data.pop("mode", None)
        scope = mode.split(":", 1)[1]
        try:
            content = await capture_portable(context.bot, hub.mother_bot_id, message, hub.media_dir(), "broadcast")
        except (ValueError, TelegramError) as exc:
            await message.reply_text(f"❌ 无法群发：{exc}")
            return True
        context.user_data["broadcast"] = {"scope": scope, "content": content}
        targets = broadcast_targets(hub, scope)
        total = sum(len(store.active_user_ids()) for _, store in targets)
        await message.reply_text(
            f"📢 确认群发？\n范围：{'所有机器人' if scope == 'all' else '仅母机器人'}（{len(targets)} 个机器人）\n"
            f"预计人数：{total}\n内容：{html.escape(content_preview(content))}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 开始群发", callback_data="dev:bcgo"),
                InlineKeyboardButton("取消", callback_data="dev:bcno"),
            ]]),
        )
        return True
    context.user_data.pop("mode", None)
    return False


def parse_user_target(message, text: str) -> int:
    origin = getattr(message, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) if origin else None
    if sender is not None:
        return int(sender.id)
    value = (text or "").strip()
    return int(value) if value.isdigit() else 0


# ------------------------------------------------------------------ 克隆
async def clone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register(update, context)
    if not is_mother(context):
        await update.effective_message.reply_text("克隆功能只在母机器人里提供。")
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("请私聊母机器人使用克隆功能。")
        return
    await clone_menu(update, context)


async def clone_menu(update: Update, context) -> None:
    user_id = uid_of(update)
    rows = [
        [InlineKeyboardButton("➕ 提交 Token 克隆", callback_data="clone:new")],
        [InlineKeyboardButton("📂 我的机器人", callback_data="clone:mine")],
    ]
    if is_developer(context, user_id):
        rows.append([InlineKeyboardButton("📋 全部子机器人", callback_data="clone:all:0")])
    await update.effective_message.reply_text(
        "🤖 克隆机器人\n\n"
        "在 @BotFather 创建一个新机器人，把 Token 发给我，审核通过后自动启动。\n"
        "提交 Token 的人就是新机器人的超级管理员。新机器人拥有和母机器人一样的搜索、收录、"
        "欢迎消息、双向功能（没有克隆功能）。",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def submit_clone_token(update: Update, context, text: str) -> None:
    hub = hub_of(context)
    message = update.effective_message
    user = update.effective_user
    match = TOKEN_PATTERN.match(text or "")
    try:
        await message.delete()  # Token 不留在聊天记录里
    except TelegramError:
        pass
    if not match:
        await context.bot.send_message(message.chat_id, "Bot Token 格式不正确，请重新点「➕ 提交 Token 克隆」。")
        return
    token = match.group(1)
    try:
        me = await hub.fetch_bot_info(token)
    except TelegramError as exc:
        await context.bot.send_message(message.chat_id, f"❌ Bot Token 验证失败：{exc}")
        return
    if int(me.id) == hub.mother_bot_id:
        await context.bot.send_message(message.chat_id, "❌ 不能克隆母机器人自己。")
        return
    existing = hub.store.child_by_bot_id(int(me.id))
    if existing is not None:
        await hub.stop_child(int(me.id))
    auto = hub.store.get_setting("clone_auto_approve", "0") == "1"
    owner_name = " ".join(x for x in (user.first_name, user.last_name) if x) or (user.username or "")
    child_id = hub.store.save_child_request(
        int(me.id), me.username or "", me.first_name or "", user.id, owner_name,
        hub.cipher.encrypt(token), approved=auto,
    )
    label = f"@{me.username}" if me.username else str(me.id)
    if auto:
        ok = await hub.start_child(hub.store.child(child_id))
        await context.bot.send_message(
            message.chat_id,
            f"✅ {label} 已克隆并启动，你是它的超级管理员。" if ok
            else f"⚠️ {label} 已登记，但启动失败：{hub.store.child(child_id)['error']}",
        )
        return
    await context.bot.send_message(message.chat_id, f"✅ 已提交克隆申请：{label}\n等待开发者审核，通过后会通知你。")
    for dev_id in hub.developer_ids():
        try:
            await context.bot.send_message(
                dev_id,
                f"📥 新的克隆申请 #{child_id}\n机器人：{html.escape(label)}（{html.escape(me.first_name or '')}）"
                f" ID <code>{me.id}</code>\n申请人：{html.escape(owner_name)}"
                + (f" @{html.escape(user.username)}" if user.username else "")
                + f" <code>{user.id}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ 通过", callback_data=f"clone:ok:{child_id}"),
                    InlineKeyboardButton("❌ 拒绝", callback_data=f"clone:no:{child_id}"),
                ]]),
            )
        except TelegramError:
            logging.info("developer %s not reachable", dev_id)


def child_label(row) -> str:
    return f"@{row['username']}" if row["username"] else str(row["bot_id"])


def child_status(hub, row) -> str:
    if row["status"] == "pending":
        return "⏳待审核"
    if row["status"] == "rejected":
        return "❌已拒绝"
    if not row["enabled"]:
        return "⏸已停用"
    if hub.is_running(int(row["bot_id"])):
        return "🟢运行中"
    return "🔴未运行" + (f"（{row['error']}）" if row["error"] else "")


def children_view(context, page: int) -> tuple[str, InlineKeyboardMarkup]:
    hub = hub_of(context)
    rows = hub.store.children()
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    mother = hub.mother_bot()
    lines = [
        f"📋 全部子机器人（共 {len(rows)} 个）",
        f"母机器人：@{html.escape(mother.username or '')} ID <code>{hub.mother_bot_id}</code>" if mother else "",
        "",
    ]
    buttons = []
    for index, row in enumerate(chunk, start=page * PAGE_SIZE + 1):
        lines.append(
            f"{index}. {html.escape(child_label(row))}｜{html.escape(row['name'] or '-')}｜"
            f"ID <code>{row['bot_id']}</code>｜{html.escape(child_status(hub, row))}"
        )
        buttons.append([InlineKeyboardButton(f"{index}. {child_label(row)}", callback_data=f"dev:kid:{row['id']}")])
    if not chunk:
        lines.append("还没有子机器人。")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"clone:all:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"clone:all:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("👑 开发者面板", callback_data="dev:home")])
    return "\n".join(x for x in lines if x is not None), InlineKeyboardMarkup(buttons)


def my_children_text(context, user_id: int) -> str:
    hub = hub_of(context)
    rows = hub.store.children_of(user_id)
    if not rows:
        return "你还没有克隆过机器人。"
    lines = ["📂 我的机器人", ""]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. {html.escape(child_label(row))}｜{html.escape(row['name'] or '-')}｜"
            f"ID <code>{row['bot_id']}</code>｜{html.escape(child_status(hub, row))}"
        )
    return "\n".join(lines)


async def clone_callback(update: Update, context, parts: list[str]) -> None:
    query = update.callback_query
    hub = hub_of(context)
    user_id = uid_of(update)
    action = parts[1] if len(parts) > 1 else ""
    if action == "new":
        context.user_data["mode"] = "token"
        await query.message.reply_text("请发送 Bot Token（在 @BotFather 创建机器人后获得）。发送 /cancel 取消。")
        return
    if action == "mine":
        await query.message.reply_text(my_children_text(context, user_id), parse_mode=ParseMode.HTML)
        return
    if not is_developer(context, user_id):
        await query.answer("只有开发者可以操作", show_alert=True)
        return
    if action == "all":
        body, markup = children_view(context, int(parts[2]) if len(parts) > 2 else 0)
        await edit_or_send(query, body, markup)
        return
    if action in {"ok", "no"}:
        child_id = int(parts[2])
        row = hub.store.child(child_id)
        if not row or not hub.store.review_child(child_id, action == "ok", user_id):
            await query.answer("该申请不存在或已审核", show_alert=True)
            return
        label = child_label(row)
        if action == "ok":
            ok = await hub.start_child(hub.store.child(child_id))
            note = f"✅ 已通过并启动 {label}" if ok else f"⚠️ 已通过，但启动失败：{hub.store.child(child_id)['error']}"
            owner_note = f"✅ 你的克隆申请 {label} 已通过审核" + ("，机器人已启动。" if ok else "，但启动失败，请联系开发者。")
        else:
            note = f"❌ 已拒绝 {label}"
            owner_note = f"❌ 你的克隆申请 {label} 未通过审核。"
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass
        await query.message.reply_text(note)
        try:
            await context.bot.send_message(int(row["owner_id"]), owner_note)
        except TelegramError:
            pass


# ------------------------------------------------------------------ 收录管理（每个机器人）
def manage_view(context) -> tuple[str, InlineKeyboardMarkup]:
    store = store_of(context)
    counts = store.counts()
    body = (
        f"🛠 收录管理 · @{html.escape(context.bot.username or '')}\n\n"
        f"已收录：{counts['approved']}\n待审核：{counts['pending']}\n"
        f"用户数：{store.user_count()}\n{storage_text(context)}"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 待审核（{counts['pending']}）", callback_data="mg:pending")],
        [InlineKeyboardButton("📚 收录列表 / 删除", callback_data="mg:list:0")],
    ])
    return body, markup


def entries_page_view(context, page: int) -> tuple[str, InlineKeyboardMarkup]:
    store = store_of(context)
    total = store.counts()["approved"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    entries = store.approved_page(page * PAGE_SIZE, PAGE_SIZE)
    lines = [f"📚 收录列表（共 {total} 条，第 {page + 1}/{pages} 页）", "点 🗑 删除对应关键词", ""]
    buttons = []
    for e in entries:
        lines.append(f"#{e.id} <code>{html.escape(e.title)}</code>")
        buttons.append([InlineKeyboardButton(f"🗑 {e.title[:30]}", callback_data=f"mg:del:{e.id}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"mg:list:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"mg:list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="mg:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def manage_callback(update: Update, context, parts: list[str]) -> None:
    query = update.callback_query
    if not is_bot_admin(context, uid_of(update)):
        await query.answer("只有本机器人的管理员可以操作", show_alert=True)
        return
    action = parts[1] if len(parts) > 1 else "home"
    store = store_of(context)
    if action == "home":
        body, markup = manage_view(context)
        await edit_or_send(query, body, markup)
    elif action == "list":
        body, markup = entries_page_view(context, int(parts[2]))
        await edit_or_send(query, body, markup)
    elif action == "del":
        store.delete_entry(int(parts[2]))
        await query.answer("已删除")
        body, markup = entries_page_view(context, int(parts[3]))
        await edit_or_send(query, body, markup)
    elif action == "pending":
        entries = store.pending_entries(limit=5)
        if not entries:
            await query.answer("没有待审核的收录", show_alert=True)
            return
        for entry in entries:
            try:
                await deliver_entry(entry, bot=context.bot, chat_id=query.message.chat_id)
            except TelegramError:
                pass
            await context.bot.send_message(
                query.message.chat_id, entry_card(entry), parse_mode=ParseMode.HTML,
                reply_markup=review_keyboard(entry.id),
            )


async def review_callback(update: Update, context, parts: list[str]) -> None:
    query = update.callback_query
    user_id = uid_of(update)
    if not is_bot_admin(context, user_id):
        await query.answer("只有本机器人的管理员可以审核", show_alert=True)
        return
    approved = parts[1] == "a"
    entry_id = int(parts[2])
    store = store_of(context)
    entry = store.get(entry_id)
    if not entry or not store.review(entry_id, approved, user_id):
        await query.answer("该收录不存在或已审核", show_alert=True)
        return
    try:
        await query.edit_message_text(
            entry_card(store.get(entry_id) or entry) + f"\n\n{'✅ 已通过' if approved else '❌ 已拒绝'}",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass
    try:
        await context.bot.send_message(
            entry.user_id,
            f"你的收录 #{entry_id}「{entry.title}」" + ("已通过审核 ✅" if approved else "未通过审核 ❌"),
        )
    except TelegramError:
        pass


# ------------------------------------------------------------------ 开发者面板（仅母机器人）
def dev_home_view(context) -> tuple[str, InlineKeyboardMarkup]:
    hub = hub_of(context)
    children = hub.store.children()
    running = sum(1 for r in children if hub.is_running(int(r["bot_id"])))
    pending = len(hub.store.children("pending"))
    users = sum(store.user_count() for _, store in hub.all_stores())
    receiver = relay.relay_receiver(hub)
    auto = hub.store.get_setting("clone_auto_approve", "0") == "1"
    body = (
        "👑 开发者面板（控制母机器人和全部子机器人）\n\n"
        f"子机器人：{len(children)} 个（运行中 {running}，待审核 {pending}）\n"
        f"所有机器人用户合计：{users}\n"
        f"双向接收人：{('<code>%d</code>' % receiver) if receiver else '未设置/已关闭'}\n"
        f"欢迎消息：{html.escape(content_preview(hub.get_json('welcome_content'))[:60])}"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 子机器人管理", callback_data="clone:all:0"),
         InlineKeyboardButton(f"📥 克隆审核（{pending}）", callback_data="dev:pending")],
        [InlineKeyboardButton("👋 欢迎消息", callback_data="dev:wel"),
         InlineKeyboardButton("💬 双向设置", callback_data="dev:relay")],
        [InlineKeyboardButton("📢 群发消息", callback_data="dev:bc"),
         InlineKeyboardButton("👥 开发者", callback_data="dev:devs")],
        [InlineKeyboardButton(f"⚙️ 克隆自动通过：{'开' if auto else '关'}", callback_data="dev:auto")],
        [InlineKeyboardButton("🔄 刷新", callback_data="dev:home")],
    ])
    return body, markup


def child_detail_view(context, child_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hub = hub_of(context)
    row = hub.store.child(child_id)
    if not row:
        return "该子机器人不存在或已删除。", InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ 返回列表", callback_data="clone:all:0")]])
    bot_id = int(row["bot_id"])
    app = hub.apps.get(bot_id)
    stats = ""
    if app is not None:
        store = app.bot_data["store"]
        counts = store.counts()
        stats = f"\n用户：{store.user_count()}　收录：{counts['approved']}（待审 {counts['pending']}）"
    size = db_size_bytes(hub.child_db_path(bot_id))
    body = (
        f"🤖 {html.escape(child_label(row))}\n"
        f"名称：{html.escape(row['name'] or '-')}\nID：<code>{bot_id}</code>\n"
        f"超级管理员：{html.escape(row['owner_name'] or '-')} <code>{row['owner_id']}</code>\n"
        f"状态：{html.escape(child_status(hub, row))}{stats}\n"
        f"存储：{fmt_bytes(size)} / {fmt_bytes(hub.config.storage_quota_bytes)}\n"
        f"创建：{row['created_at']} (UTC)"
    )
    toggle = (InlineKeyboardButton("⏸ 停用", callback_data=f"dev:off:{child_id}") if row["enabled"]
              else InlineKeyboardButton("▶️ 启用", callback_data=f"dev:on:{child_id}"))
    rows = []
    if row["status"] == "pending":
        rows.append([InlineKeyboardButton("✅ 通过", callback_data=f"clone:ok:{child_id}"),
                     InlineKeyboardButton("❌ 拒绝", callback_data=f"clone:no:{child_id}")])
    else:
        rows.append([toggle, InlineKeyboardButton("🔄 重启", callback_data=f"dev:restart:{child_id}")])
    rows.append([InlineKeyboardButton("🗑 删除", callback_data=f"dev:del:{child_id}")])
    rows.append([InlineKeyboardButton("⬅️ 返回列表", callback_data="clone:all:0")])
    return body, InlineKeyboardMarkup(rows)


def welcome_view(context) -> tuple[str, InlineKeyboardMarkup]:
    hub = hub_of(context)
    sync = hub.store.get_setting("welcome_sync_description", "1") == "1"
    body = (
        "👋 欢迎消息（页面自动消息）\n\n"
        "设置后，任何人点开母机器人或任意子机器人（/start）都会看到这条内容，"
        "支持文字格式、图片、视频、文件和按钮，原样显示。\n\n"
        f"当前：{html.escape(content_preview(hub.get_json('welcome_content')))}\n"
        f"同步到机器人简介页（没点开始前看到的介绍，仅文字）：{'开' if sync else '关'}"
    )
    return body, InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 设置欢迎消息", callback_data="dev:welset"),
         InlineKeyboardButton("👀 预览", callback_data="dev:welview")],
        [InlineKeyboardButton(f"📝 同步简介：{'开' if sync else '关'}", callback_data="dev:welsync"),
         InlineKeyboardButton("🗑 清除", callback_data="dev:welclr")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="dev:home")],
    ])


def relay_view(context) -> tuple[str, InlineKeyboardMarkup]:
    hub = hub_of(context)
    raw = hub.store.get_setting("relay_receiver", "")
    enabled = hub.store.get_setting("relay_enabled", "1") == "1"
    autodel = hub.store.get_setting("relay_autodelete", "1") == "1"
    hours = hub.config.relay_delete_seconds // 3600
    body = (
        "💬 双向设置（所有机器人共用）\n\n"
        "用户在母机器人或任意子机器人里发的消息，都由母机器人转给下面的接收人；"
        "接收人直接「回复」那条消息，就会通过用户当时聊天的机器人回给用户。\n\n"
        f"接收人：{('<code>' + raw + '</code>') if raw else '未设置'}\n"
        f"双向：{'开' if enabled else '关'}\n"
        f"转发消息 {hours} 小时后自动删除：{'开' if autodel else '关'}\n\n"
        "⚠️ 接收人必须先私聊母机器人点一次 /start。"
    )
    return body, InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 设置接收人", callback_data="dev:rcv"),
         InlineKeyboardButton("🙋 设为我自己", callback_data="dev:rcvme")],
        [InlineKeyboardButton(f"双向：{'开' if enabled else '关'}", callback_data="dev:relaytog"),
         InlineKeyboardButton(f"自动删除：{'开' if autodel else '关'}", callback_data="dev:deltog")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="dev:home")],
    ])


def devs_view(context) -> tuple[str, InlineKeyboardMarkup]:
    hub = hub_of(context)
    fixed = sorted(hub.config.developer_ids)
    extra = [x for x in hub.store.extra_developers() if x not in hub.config.developer_ids]
    lines = ["👥 开发者（在所有机器人上拥有全部权限）", ""]
    lines += [f"• <code>{x}</code>（安装时设置，不能在这里删除）" for x in fixed]
    lines += [f"• <code>{x}</code>" for x in extra]
    rows = [[InlineKeyboardButton(f"➖ 移除 {x}", callback_data=f"dev:devrm:{x}")] for x in extra]
    rows.append([InlineKeyboardButton("➕ 添加开发者", callback_data="dev:devadd")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="dev:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def broadcast_targets(hub, scope: str):
    stores = hub.all_stores()
    if scope != "all":
        stores = [item for item in stores if item[0] == hub.mother_bot_id]
    return stores


async def run_broadcast(hub, content: dict, scope: str, report_chat: int, report_bot) -> dict:
    sent = failed = blocked = 0
    for bot_id, store in broadcast_targets(hub, scope):
        bot = hub.bot_for(bot_id)
        if bot is None:
            continue
        for user_id in store.active_user_ids():
            try:
                await send_portable(bot, bot_id, user_id, content, hub.store)
                sent += 1
            except Forbidden:
                store.mark_user_blocked(user_id)
                blocked += 1
            except (TelegramError, ValueError):
                failed += 1
            await asyncio.sleep(0.05)
    result = {"sent": sent, "failed": failed, "blocked": blocked}
    try:
        await report_bot.send_message(
            report_chat, f"📢 群发完成：成功 {sent}，失败 {failed}，已屏蔽 {blocked}",
        )
    except TelegramError:
        pass
    return result


async def dev_callback(update: Update, context, parts: list[str]) -> None:
    query = update.callback_query
    hub = hub_of(context)
    user_id = uid_of(update)
    if not (is_mother(context) and is_developer(context, user_id)):
        await query.answer("只有开发者可以操作", show_alert=True)
        return
    action = parts[1] if len(parts) > 1 else "home"
    arg = parts[2] if len(parts) > 2 else ""
    if action == "home":
        await edit_or_send(query, *dev_home_view(context))
    elif action == "auto":
        current = hub.store.get_setting("clone_auto_approve", "0") == "1"
        hub.store.set_setting("clone_auto_approve", "0" if current else "1")
        await edit_or_send(query, *dev_home_view(context))
    elif action == "pending":
        rows = hub.store.children("pending")
        if not rows:
            await query.answer("没有待审核的克隆申请", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(f"{child_label(r)}（申请人 {r['owner_id']}）",
                                         callback_data=f"dev:kid:{r['id']}")] for r in rows]
        buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="dev:home")])
        await edit_or_send(query, f"📥 待审核克隆申请：{len(rows)} 个", InlineKeyboardMarkup(buttons))
    elif action == "kid":
        await edit_or_send(query, *child_detail_view(context, int(arg)))
    elif action in {"on", "off", "restart"}:
        child_id = int(arg)
        row = hub.store.child(child_id)
        if not row:
            await query.answer("不存在", show_alert=True)
            return
        if action == "off":
            hub.store.set_child_enabled(child_id, False)
            await hub.stop_child(int(row["bot_id"]))
            await query.answer("已停用")
        else:
            hub.store.set_child_enabled(child_id, True)
            ok = await hub.start_child(hub.store.child(child_id))
            await query.answer("已启动" if ok else "启动失败，见状态", show_alert=not ok)
        await edit_or_send(query, *child_detail_view(context, child_id))
    elif action == "del":
        row = hub.store.child(int(arg))
        if not row:
            await query.answer("不存在", show_alert=True)
            return
        await edit_or_send(
            query,
            f"⚠️ 确定删除 {html.escape(child_label(row))}？\n机器人会停止运行，它的收录数据会归档到 data/bots/deleted/。",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 确认删除", callback_data=f"dev:delok:{arg}"),
                InlineKeyboardButton("取消", callback_data=f"dev:kid:{arg}"),
            ]]),
        )
    elif action == "delok":
        ok = await hub.delete_child(int(arg))
        await query.answer("已删除" if ok else "不存在")
        await edit_or_send(query, *children_view(context, 0))
    elif action == "wel":
        await edit_or_send(query, *welcome_view(context))
    elif action == "welset":
        context.user_data["mode"] = "dev_welcome"
        await query.message.reply_text(
            "请发送欢迎消息内容（文字/图片/视频/文件都可以，格式和按钮原样保留；"
            "也可以转发一条带按钮的频道消息）。发送 /cancel 取消。"
        )
    elif action == "welview":
        content = hub.get_json("welcome_content")
        if not content:
            await query.answer("还没有设置欢迎消息", show_alert=True)
            return
        await send_portable(context.bot, hub.mother_bot_id, query.message.chat_id, content, hub.store)
    elif action == "welclr":
        hub.set_json("welcome_content", None)
        await hub.apply_description_all(force=True)
        await query.answer("已清除，恢复默认欢迎语")
        await edit_or_send(query, *welcome_view(context))
    elif action == "welsync":
        current = hub.store.get_setting("welcome_sync_description", "1") == "1"
        hub.store.set_setting("welcome_sync_description", "0" if current else "1")
        if not current:
            await hub.apply_description_all()
        await edit_or_send(query, *welcome_view(context))
    elif action == "relay":
        await edit_or_send(query, *relay_view(context))
    elif action == "rcv":
        context.user_data["mode"] = "dev_receiver"
        await query.message.reply_text("请发送接收人的数字 ID，或转发一条他的消息。发送 /cancel 取消。")
    elif action == "rcvme":
        hub.store.set_setting("relay_receiver", str(user_id))
        await query.answer("已设为你自己")
        await edit_or_send(query, *relay_view(context))
    elif action == "relaytog":
        current = hub.store.get_setting("relay_enabled", "1") == "1"
        hub.store.set_setting("relay_enabled", "0" if current else "1")
        await edit_or_send(query, *relay_view(context))
    elif action == "deltog":
        current = hub.store.get_setting("relay_autodelete", "1") == "1"
        hub.store.set_setting("relay_autodelete", "0" if current else "1")
        await edit_or_send(query, *relay_view(context))
    elif action == "bc":
        if arg in {"all", "mother"}:
            context.user_data["mode"] = f"dev_broadcast:{arg}"
            await query.message.reply_text("请发送要群发的内容（文字/图片/视频/文件/按钮）。发送 /cancel 取消。")
            return
        await edit_or_send(query, "📢 群发消息：选择范围", InlineKeyboardMarkup([
            [InlineKeyboardButton("所有机器人的用户", callback_data="dev:bc:all")],
            [InlineKeyboardButton("仅母机器人的用户", callback_data="dev:bc:mother")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="dev:home")],
        ]))
    elif action == "bcgo":
        job = context.user_data.pop("broadcast", None)
        if not job:
            await query.answer("没有待发送的内容", show_alert=True)
            return
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass
        await query.message.reply_text("📢 开始群发，完成后会通知你。")
        context.application.create_task(
            run_broadcast(hub, job["content"], job["scope"], query.message.chat_id, context.bot)
        )
    elif action == "bcno":
        context.user_data.pop("broadcast", None)
        try:
            await query.edit_message_text("已取消群发。")
        except TelegramError:
            pass
    elif action == "devs":
        await edit_or_send(query, *devs_view(context))
    elif action == "devadd":
        context.user_data["mode"] = "dev_adddev"
        await query.message.reply_text("请发送新开发者的数字 ID，或转发一条他的消息。发送 /cancel 取消。")
    elif action == "devrm":
        hub.store.remove_developer(int(arg))
        await edit_or_send(query, *devs_view(context))


async def dev_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (is_mother(context) and is_developer(context, uid_of(update))):
        return
    body, markup = dev_home_view(context)
    await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)


# ------------------------------------------------------------------ 公共
async def edit_or_send(query, text: str, markup=None) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      disable_web_page_preview=True)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    try:
        if parts[0] == "clone" and is_mother(context):
            await clone_callback(update, context, parts)
        elif parts[0] == "dev":
            await dev_callback(update, context, parts)
        elif parts[0] == "mg":
            await manage_callback(update, context, parts)
        elif parts[0] == "rv":
            await review_callback(update, context, parts)
    finally:
        try:
            await query.answer()
        except TelegramError:
            pass


async def process_deletions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """定时删除（双向消息 47 小时后删；Telegram 只允许删 48 小时内的消息）。"""
    hub = hub_of(context)
    for row in hub.store.due_deletions():
        bot = hub.bot_for(int(row["bot_id"]))
        if bot is None:
            hub.store.finish_deletion(int(row["id"]))
            continue
        try:
            await bot.delete_message(int(row["chat_id"]), int(row["message_id"]))
        except BadRequest:
            hub.store.finish_deletion(int(row["id"]))
        except TelegramError:
            hub.store.finish_deletion(int(row["id"]), retry=True)
            continue
        else:
            hub.store.finish_deletion(int(row["id"]))
        hub.store.forget_relay(int(row["bot_id"]), int(row["chat_id"]), int(row["message_id"]))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("update failed", exc_info=context.error)


def register_handlers(app: Application) -> None:
    private = filters.ChatType.PRIVATE
    groups = filters.ChatType.GROUPS
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("my", my_cmd))
    app.add_handler(CommandHandler("clone", clone_cmd, filters=private))
    app.add_handler(CommandHandler("dev", dev_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, private_message))
    app.add_handler(MessageHandler(groups & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, group_message))
    app.add_error_handler(error_handler)


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    register_handlers(app)
    return app
