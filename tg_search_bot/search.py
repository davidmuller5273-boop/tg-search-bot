"""搜索 / 收录（照搬 tg-directory-bot 的规则）。

* 发送关键词：整条消息等于关键词才回复（「XX」「XX地址」都行），不会误触发。
* 「关键词 搜录 内容」或回复一条消息发送「关键词 搜录」：原样收录（格式、媒体、按钮）。
* 同一关键词审核通过后以最新一条为准。
"""
from __future__ import annotations

import html
import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError

from .content import parse_entities
from .rich_content import (
    buttons_markup, capture_buttons, capture_buttons_resolving, capture_content,
    forward_channel_source,
)
from .storage import Entry, SearchStore

DIRECTORY_TRIGGER = "地址"
NOT_FOUND_TEXT = "没有收录该关键词，请联系管理员。"
DIRECTORY_NOT_FOUND_MAX_QUERY = 20

RICH_SUBMISSION_PATTERN = re.compile(r"\s*(.{1,120}?)\s+搜录(?:\s+([\s\S]*?))?\s*")
RICH_SUBMISSION_MARK = re.compile(r"\s+搜录(?:\s+|$)")

ENTRY_MEDIA_METHODS = {
    "photo": "photo", "video": "video", "animation": "animation",
    "audio": "audio", "voice": "voice", "document": "document",
    "sticker": "sticker", "video_note": "video_note",
}


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def parse_rich_submission_command(text: str) -> tuple[str, str] | None:
    """「关键词 搜录 内容」 或 「关键词 搜录」（回复要收录的消息）。"""
    match = RICH_SUBMISSION_PATTERN.fullmatch(text or "")
    if not match:
        return None
    keyword = " ".join(match.group(1).split())
    content = (match.group(2) or "").strip()
    return (keyword, content) if keyword else None


def slice_submission_content(message) -> tuple[str, str]:
    """「搜录」后面的文字及其格式（UTF-16 偏移平移）。"""
    full_text, raw_entities = capture_content(message)
    match = RICH_SUBMISSION_MARK.search(full_text or "")
    if not match:
        return "", "[]"
    start = match.end()
    end = len(full_text.rstrip())
    if end <= start:
        return "", "[]"
    content = full_text[start:end]
    start16 = _utf16_len(full_text[:start])
    end16 = start16 + _utf16_len(content)
    entities = []
    for item in json.loads(raw_entities or "[]"):
        offset = int(item.get("offset", 0))
        length = int(item.get("length", 0))
        left, right = max(offset, start16), min(offset + length, end16)
        if right <= left:
            continue
        shifted = dict(item)
        shifted["offset"] = left - start16
        shifted["length"] = right - left
        entities.append(shifted)
    return content, json.dumps(entities, ensure_ascii=False)


def publishing_attachment(message) -> tuple[str, str, str]:
    for kind in ("sticker", "video_note", "animation", "document", "photo", "video", "audio", "voice"):
        value = getattr(message, kind, None)
        if not value:
            continue
        if kind == "photo":
            return value[-1].file_id, "photo", ""
        return value.file_id, kind, str(getattr(value, "file_name", "") or "")
    return "", "", ""


def parse_group_trigger(content: str, directory_trigger: str = DIRECTORY_TRIGGER) -> str | None:
    text = content.strip()
    key = directory_trigger.strip()
    if key and text.casefold().endswith(key.casefold()):
        query = text[:-len(key)].strip()
        if query:
            return query
    return None


def is_directory_not_found_query(query: str) -> bool:
    """只有简短的「XX地址」才回复没有收录；长句碰巧以地址结尾不回复。"""
    query = query.strip()
    return bool(query) and "\n" not in query and len(query) <= DIRECTORY_NOT_FOUND_MAX_QUERY


def entry_body(entry: Entry) -> tuple[str, list]:
    if entry.content_text:
        return entry.content_text, parse_entities(entry.entities_json)
    return "", []


async def deliver_entry(entry: Entry, *, message=None, bot=None, chat_id: int | None = None) -> None:
    """原样发送收录：先 copy_message 原消息，失败再用保存的文字+格式+媒体+按钮重建。"""
    markup = buttons_markup(entry.buttons_json)

    def call(kind: str, *args, **kwargs):
        if message is not None:
            name = "reply_text" if kind == "message" else f"reply_{kind}"
            return getattr(message, name)(*args, **kwargs)
        return getattr(bot, f"send_{kind}")(chat_id, *args, **kwargs)

    if entry.copy_chat_id and entry.copy_message_id:
        kwargs = {"reply_markup": markup} if markup else {}
        try:
            if message is not None:
                await message.reply_copy(entry.copy_chat_id, entry.copy_message_id, **kwargs)
            else:
                await bot.copy_message(
                    chat_id=chat_id, from_chat_id=entry.copy_chat_id,
                    message_id=entry.copy_message_id, **kwargs,
                )
            return
        except TelegramError:
            logging.info("copy_message failed for entry %s; rebuilding", entry.id)
    text, entities = entry_body(entry)
    entity_kwargs = {"entities": entities} if entities else {}
    if not entry.media_file_id:
        await call("message", text or entry.title, parse_mode=None, reply_markup=markup, **entity_kwargs)
        return
    kind = ENTRY_MEDIA_METHODS.get(entry.media_type, "document")
    if kind in {"sticker", "video_note"}:
        if text:
            await call("message", text, parse_mode=None, **entity_kwargs)
        await call(kind, entry.media_file_id, reply_markup=markup)
        return
    if text and len(text) > 1024:
        await call(kind, entry.media_file_id)
        await call("message", text, parse_mode=None, reply_markup=markup, **entity_kwargs)
        return
    caption_kwargs = {"caption_entities": entities} if entities else {}
    await call(kind, entry.media_file_id, caption=text or None, parse_mode=None,
               reply_markup=markup, **caption_kwargs)


def entry_keyword_list_text(entries: list[Entry]) -> str:
    lines = ["🔎 找到以下关键词，发送关键词即可查看内容：", ""]
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. <code>{html.escape(entry.title)}</code>")
    return "\n".join(lines)


def entry_card(entry: Entry) -> str:
    labels = {"pending": "待审核", "approved": "已通过", "rejected": "已拒绝"}
    content = entry.content_text.strip()
    if len(content) > 300:
        content = content[:300] + "…"
    lines = [f"<b>收录 #{entry.id}</b>", f"关键词：{html.escape(entry.title)}"]
    if content:
        lines.append(f"内容：{html.escape(content)}")
    if entry.media_type:
        lines.append(f"附件：{html.escape(entry.media_type)}")
    if entry.copy_message_id and not content and not entry.media_type:
        lines.append("内容：原样转存的消息（见上方）")
    lines.append(f"状态：{labels.get(entry.status, entry.status)}")
    lines.append(
        f"提交者：<code>{entry.user_id}</code>"
        + (f" @{html.escape(entry.username)}" if entry.username else "")
    )
    return "\n".join(lines)


def review_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 通过", callback_data=f"rv:a:{entry_id}"),
        InlineKeyboardButton("❌ 拒绝", callback_data=f"rv:r:{entry_id}"),
    ]])


async def search_reply(message, store: SearchStore, query: str, user_id: int = 0) -> None:
    """明确搜索（/search、🔎 搜索）：先精确匹配，否则列出最多 10 个关键词。"""
    entry = store.find_keyword_entry(query, (DIRECTORY_TRIGGER,))
    if entry:
        store.record_search(user_id, query, 1)
        await deliver_entry(entry, message=message)
        return
    entries = store.search_keyword_titles(query, limit=10)
    store.record_search(user_id, query, len(entries))
    if not entries:
        await message.reply_text(NOT_FOUND_TEXT)
        return
    await message.reply_text(entry_keyword_list_text(entries), parse_mode=ParseMode.HTML)


async def keyword_auto_reply(message, store: SearchStore, text: str, user_id: int = 0) -> bool:
    """普通消息里的关键词：精确命中就回复；简短「XX地址」没收录就提示。True = 已处理。"""
    entry = store.find_keyword_entry(text, (DIRECTORY_TRIGGER,))
    if entry:
        store.record_search(user_id, text, 1)
        await deliver_entry(entry, message=message)
        return True
    query = parse_group_trigger(text)
    if query and is_directory_not_found_query(query):
        store.record_search(user_id, query, 0)
        await message.reply_text(NOT_FOUND_TEXT)
        return True
    return False


async def build_submission(bot, message, chat, keyword: str, content: str) -> dict | None:
    """解析一条收录提交，返回 add_rich_submission 的参数；格式不对返回 None。"""
    file_id, file_type, file_name = publishing_attachment(message)
    target = getattr(message, "reply_to_message", None)
    if target is not None and getattr(target, "forum_topic_created", None):
        target = None
    entities_json = "[]"
    buttons_json = "[]"
    copy_chat_id = copy_message_id = 0
    if not content and not file_id and target is not None:
        content, entities_json = capture_content(target)
        file_id, file_type, file_name = publishing_attachment(target)
        scratch = chat.id if chat.type == ChatType.PRIVATE else 0
        try:
            buttons_json = (
                await capture_buttons_resolving(bot, target, scratch) if scratch else capture_buttons(target)
            )
        except Exception:
            buttons_json = "[]"
        source = forward_channel_source(target)
        copy_chat_id, copy_message_id = source or (chat.id, target.message_id)
    elif content or file_id:
        if content:
            content, entities_json = slice_submission_content(message)
    else:
        return None
    return dict(
        keyword=keyword, unique_url=f"tgcontent://{chat.id}/{message.message_id}",
        content_text=content, file_id=file_id, file_type=file_type, file_name=file_name,
        entities_json=entities_json, buttons_json=buttons_json,
        copy_chat_id=copy_chat_id, copy_message_id=copy_message_id,
    )
