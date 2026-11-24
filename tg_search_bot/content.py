"""跨机器人的消息内容。

Telegram 的 file_id 只能被拿到它的那个机器人使用，所以：
* 欢迎消息 / 群发：开发者在母机器人里发的媒体会下载保存一份，
  每个子机器人第一次发送时上传，之后缓存自己的 file_id。
* 双向转发：消息在两个不同机器人之间转交时，媒体临时下载再上传；
  同一个机器人内直接 copy_message（原样保留）。
"""
from __future__ import annotations

import io
import json
import logging
import uuid
from pathlib import Path

from telegram import InputFile, MessageEntity
from telegram.error import TelegramError

from .rich_content import buttons_markup, capture_buttons, capture_content

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API 下载上限

MEDIA_KINDS = ("animation", "photo", "video", "document", "audio", "voice", "sticker", "video_note")
MEDIA_LABELS = {
    "photo": "图片", "video": "视频", "animation": "动图", "document": "文件",
    "audio": "音频", "voice": "语音", "sticker": "贴纸", "video_note": "视频消息",
}
DEFAULT_FILENAMES = {
    "photo": "photo.jpg", "video": "video.mp4", "animation": "animation.mp4",
    "document": "file.bin", "audio": "audio.mp3", "voice": "voice.ogg",
    "sticker": "sticker.webp", "video_note": "video_note.mp4",
}


def message_media(message) -> tuple[str, str, str, int]:
    """(kind, file_id, file_name, file_size)；没有媒体时 kind 为空。"""
    message = getattr(message, "original_message", message)
    for kind in MEDIA_KINDS:
        value = getattr(message, kind, None)
        if not value:
            continue
        if kind == "photo":
            value = value[-1]
        name = str(getattr(value, "file_name", "") or "")
        if kind == "sticker":
            if getattr(value, "is_animated", False):
                name = "sticker.tgs"
            elif getattr(value, "is_video", False):
                name = "sticker.webm"
        return kind, str(value.file_id), name, int(getattr(value, "file_size", 0) or 0)
    return "", "", "", 0


def message_summary(message) -> str:
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    kind = message_media(message)[0]
    if kind:
        return f"[{MEDIA_LABELS.get(kind, '媒体')}] {text}".strip()
    return text or "[消息]"


def parse_entities(raw: str) -> list[MessageEntity]:
    try:
        return [MessageEntity.de_json(item, None) for item in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


async def _send_media(bot, kind: str, chat_id: int, media, text: str, entities, markup, reply_parameters=None):
    common = {"reply_markup": markup}
    if reply_parameters is not None:
        common["reply_parameters"] = reply_parameters
    if kind in {"sticker", "video_note"}:
        sent_text = None
        if text:
            sent_text = await bot.send_message(chat_id, text, entities=entities or None, parse_mode=None,
                                               reply_parameters=reply_parameters)
        method = getattr(bot, f"send_{kind}")
        sent = await method(chat_id, media, **common)
        return sent or sent_text
    method = getattr(bot, f"send_{kind}")
    if text and len(text) > 1024:
        await method(chat_id, media, reply_parameters=reply_parameters)
        return await bot.send_message(chat_id, text, entities=entities or None, parse_mode=None,
                                      reply_markup=markup)
    return await method(
        chat_id, media, caption=text or None, caption_entities=entities or None,
        parse_mode=None, **common,
    )


def sent_file_id(message, kind: str) -> str:
    if message is None:
        return ""
    value = getattr(message, kind, None)
    if not value:
        return ""
    if kind == "photo":
        value = value[-1]
    return str(getattr(value, "file_id", "") or "")


# ---------------------------------------------------------------- 保存的内容
async def capture_portable(bot, bot_id: int, message, media_dir: Path, name: str) -> dict:
    """把一条消息（文字/媒体/格式/按钮）保存成所有机器人都能发送的内容。"""
    text, entities_json = capture_content(message)
    buttons_json = capture_buttons(message)
    kind, file_id, file_name, size = message_media(message)
    key = f"{name}-{uuid.uuid4().hex[:12]}"
    content = {
        "key": key, "text": text or "", "entities_json": entities_json,
        "buttons_json": buttons_json, "media_type": kind, "media_name": file_name,
        "media_path": "", "src_bot_id": int(bot_id), "src_file_id": file_id,
    }
    if not kind and not (text or "").strip():
        raise ValueError("请发送文字、图片、视频或文件")
    if kind:
        if size and size > MAX_DOWNLOAD_BYTES:
            raise ValueError("媒体超过 20MB，子机器人无法使用，请换一个小一点的文件")
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"{key}.bin"
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=path)
        content["media_path"] = str(path)
    return content


def content_preview(content: dict | None) -> str:
    if not content:
        return "（未设置）"
    parts = []
    if content.get("media_type"):
        parts.append(f"[{MEDIA_LABELS.get(content['media_type'], '媒体')}]")
    text = str(content.get("text") or "").strip()
    if text:
        parts.append(text[:200] + ("…" if len(text) > 200 else ""))
    buttons = json.loads(content.get("buttons_json") or "[]")
    if buttons:
        parts.append(f"（{sum(len(r) for r in buttons)} 个按钮）")
    return " ".join(parts) or "（空）"


async def send_portable(bot, bot_id: int, chat_id: int, content: dict, hub, reply_markup=None):
    """用任意机器人发送保存的内容；媒体首次上传后缓存该机器人的 file_id。"""
    text = str(content.get("text") or "")
    entities = parse_entities(content.get("entities_json") or "[]")
    markup = buttons_markup(content.get("buttons_json") or "[]") or reply_markup
    kind = str(content.get("media_type") or "")
    if not kind:
        return await bot.send_message(chat_id, text, entities=entities or None, parse_mode=None,
                                      reply_markup=markup)
    key = str(content.get("key") or "")
    file_id = ""
    if int(content.get("src_bot_id") or 0) == int(bot_id):
        file_id = str(content.get("src_file_id") or "")
    if not file_id and key:
        file_id = hub.cached_file_id(bot_id, key)
    if file_id:
        try:
            return await _send_media(bot, kind, chat_id, file_id, text, entities, markup)
        except TelegramError as exc:
            if "file" not in str(exc).lower() and "wrong" not in str(exc).lower():
                raise
            logging.info("cached file_id rejected for bot %s, re-uploading", bot_id)
    path = Path(str(content.get("media_path") or ""))
    if not path.is_file():
        raise ValueError("媒体文件丢失，请重新设置内容")
    filename = str(content.get("media_name") or DEFAULT_FILENAMES.get(kind, "file.bin"))
    with path.open("rb") as handle:
        sent = await _send_media(bot, kind, chat_id, InputFile(handle, filename=filename), text, entities, markup)
    new_id = sent_file_id(sent, kind)
    if new_id and key:
        hub.cache_file_id(bot_id, key, new_id)
    return sent


# ---------------------------------------------------------------- 双向转交
async def transfer_message(
    src_bot, src_bot_id: int, message, dst_bot, dst_bot_id: int, chat_id: int,
    reply_to: int | None = None,
):
    """把 message（在 src_bot 收到的）用 dst_bot 发给 chat_id，尽量原样。"""
    from telegram import ReplyParameters

    reply_parameters = (
        ReplyParameters(message_id=int(reply_to), allow_sending_without_reply=True) if reply_to else None
    )
    if int(src_bot_id) == int(dst_bot_id):
        return await dst_bot.copy_message(
            chat_id, message.chat_id, message.message_id, reply_parameters=reply_parameters,
        )
    text, entities_json = capture_content(message)
    entities = parse_entities(entities_json)
    markup = buttons_markup(capture_buttons(message))
    kind, file_id, file_name, size = message_media(message)
    if not kind:
        if not (text or "").strip():
            text = "[此类消息无法跨机器人转交，请让对方发送文字或媒体]"
            entities = []
        return await dst_bot.send_message(
            chat_id, text, entities=entities or None, parse_mode=None,
            reply_markup=markup, reply_parameters=reply_parameters,
        )
    if size and size > MAX_DOWNLOAD_BYTES:
        note = f"[{MEDIA_LABELS.get(kind, '媒体')}超过 20MB，无法跨机器人转交]"
        return await dst_bot.send_message(
            chat_id, (note + "\n" + text).strip(), parse_mode=None, reply_parameters=reply_parameters,
        )
    tg_file = await src_bot.get_file(file_id)
    data = bytes(await tg_file.download_as_bytearray())
    filename = file_name or DEFAULT_FILENAMES.get(kind, "file.bin")
    upload = InputFile(io.BytesIO(data), filename=filename)
    return await _send_media(dst_bot, kind, chat_id, upload, text, entities, markup, reply_parameters)
