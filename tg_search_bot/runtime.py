"""单进程运行母机器人和所有子机器人。

所有机器人在同一个 asyncio 循环里各自轮询，所以双向消息可以直接
“用母机器人转给接收人、再用原来的子机器人回给用户”。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

from telegram import Bot, BotCommand, Update
from telegram.error import Conflict, Forbidden, InvalidToken, TelegramError
from telegram.ext import Application

from .config import Config
from .storage import HubStore, SearchStore

ALLOWED_UPDATES = [Update.MESSAGE, Update.CALLBACK_QUERY]


def token_bot_id(token: str) -> int:
    head = str(token or "").split(":", 1)[0].strip()
    return int(head) if head.isdigit() else 0


class TokenCipher:
    """子机器人 Token 加密保存（与 tg-directory-bot 相同的算法）。"""

    def __init__(self, key: bytes):
        self.key = key

    def _stream(self, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(hashlib.sha256(self.key + nonce + counter.to_bytes(4, "big")).digest())
            counter += 1
        return bytes(output[:length])

    def encrypt(self, token: str) -> str:
        nonce = secrets.token_bytes(16)
        raw = token.encode("utf-8")
        encrypted = bytes(a ^ b for a, b in zip(raw, self._stream(nonce, len(raw))))
        tag = hmac.new(self.key, nonce + encrypted, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + tag + encrypted).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            payload = base64.urlsafe_b64decode(value.encode("ascii"))
            nonce, tag, encrypted = payload[:16], payload[16:48], payload[48:]
            expected = hmac.new(self.key, nonce + encrypted, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise ValueError
            return bytes(a ^ b for a, b in zip(encrypted, self._stream(nonce, len(encrypted)))).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("子机器人 Token 无法解密，请重新提交 Token") from exc


class Hub:
    def __init__(self, config: Config):
        self.config = config
        self.data_dir = Path(config.data_dir)
        self.store = HubStore(self.data_dir / "hub.sqlite3")
        self.store.init()
        self.mother_store = SearchStore(self.data_dir / "bots" / "mother.sqlite3")
        self.mother_store.init()
        self.cipher = TokenCipher(config.cipher_key)
        self.mother_bot_id = token_bot_id(config.bot_token)
        self.mother_app: Application | None = None
        self.apps: dict[int, Application] = {}  # bot_id -> app（含母机器人）
        self._lock = asyncio.Lock()
        self.app_factory = None  # 测试可替换：(token) -> Application

    # ---------------------------------------------------------------- 角色
    def developer_ids(self) -> set[int]:
        return set(self.config.developer_ids) | set(self.store.extra_developers())

    def is_developer(self, user_id: int | None) -> bool:
        return bool(user_id) and int(user_id) in self.developer_ids()

    # ---------------------------------------------------------------- 存储
    def child_db_path(self, bot_id: int) -> Path:
        return self.data_dir / "bots" / f"{int(bot_id)}.sqlite3"

    def media_dir(self) -> Path:
        return self.data_dir / "media"

    def get_json(self, key: str) -> dict | None:
        raw = self.store.get_setting(key)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def set_json(self, key: str, value: dict | None) -> None:
        self.store.set_setting(key, json.dumps(value, ensure_ascii=False) if value else "")

    # ---------------------------------------------------------------- 机器人
    def bot_for(self, bot_id: int):
        app = self.apps.get(int(bot_id))
        return app.bot if app else None

    def mother_bot(self):
        return self.mother_app.bot if self.mother_app else None

    def is_running(self, bot_id: int) -> bool:
        app = self.apps.get(int(bot_id))
        return bool(app and app.running)

    _polling = True  # 测试里关掉真实轮询

    def all_stores(self) -> list[tuple[int, SearchStore]]:
        items = [(self.mother_bot_id, self.mother_store)]
        for bot_id, app in self.apps.items():
            if bot_id != self.mother_bot_id:
                items.append((bot_id, app.bot_data["store"]))
        return items

    async def fetch_bot_info(self, token: str):
        async with Bot(token) as bot:
            return await bot.get_me()

    def _build(self, token: str, store: SearchStore, is_mother: bool, child_row=None) -> Application:
        from .handlers import build_application

        if self.app_factory:
            app = self.app_factory(token)
            from .handlers import register_handlers
            register_handlers(app)
        else:
            app = build_application(token)
        app.bot_data.update(
            hub=self, store=store, is_mother=is_mother,
            bot_id=token_bot_id(token),
            owner_id=int(child_row["owner_id"]) if child_row is not None else 0,
            child_id=int(child_row["id"]) if child_row is not None else 0,
            quota=0 if is_mother else int(self.config.storage_quota_bytes),
        )
        return app

    async def _launch(self, app: Application, error_callback=None) -> None:
        await app.initialize()
        if self._polling and app.updater is not None:
            await app.updater.start_polling(
                allowed_updates=ALLOWED_UPDATES, error_callback=error_callback,
            )
        await app.start()

    async def _shutdown(self, app: Application) -> None:
        try:
            if app.updater is not None and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception:
            logging.exception("stopping bot failed")

    async def start_mother(self) -> Application:
        app = self._build(self.config.bot_token, self.mother_store, True)
        await self._launch(app)
        self.mother_app = app
        self.apps[self.mother_bot_id] = app
        if app.job_queue is not None:
            from .handlers import process_deletions
            app.job_queue.run_repeating(process_deletions, interval=20, first=20)
        await self.setup_bot(app)
        return app

    async def start_child(self, child_row) -> bool:
        """启动一个子机器人；失败时把原因记到登记表。"""
        child_id = int(child_row["id"])
        bot_id = int(child_row["bot_id"])
        async with self._lock:
            old = self.apps.pop(bot_id, None)
            if old:
                await self._shutdown(old)
            try:
                token = self.cipher.decrypt(str(child_row["token_cipher"]))
                store = SearchStore(self.child_db_path(bot_id))
                store.init()
                app = self._build(token, store, False, child_row)

                def on_error(exc: TelegramError) -> None:
                    if isinstance(exc, Conflict):
                        self.store.set_child_error(child_id, "Token 正在别处运行（冲突），请停掉其他程序")
                    elif isinstance(exc, (InvalidToken, Forbidden)):
                        self.store.set_child_error(child_id, f"Token 失效：{exc}")

                await self._launch(app, on_error)
            except (InvalidToken, TelegramError, ValueError, OSError) as exc:
                self.store.set_child_error(child_id, str(exc) or exc.__class__.__name__)
                logging.warning("child bot %s failed to start: %s", bot_id, exc)
                return False
            self.apps[bot_id] = app
            me = app.bot
            try:
                self.store.update_child_profile(child_id, me.username or "", me.first_name or "")
            except Exception:
                pass
            self.store.set_child_error(child_id, "")
        await self.setup_bot(app)
        return True

    async def stop_child(self, bot_id: int) -> None:
        async with self._lock:
            app = self.apps.pop(int(bot_id), None)
            if app:
                await self._shutdown(app)

    async def delete_child(self, child_id: int) -> bool:
        row = self.store.child(child_id)
        if not row:
            return False
        await self.stop_child(int(row["bot_id"]))
        self.store.delete_child(child_id)
        archive = self.data_dir / "bots" / "deleted"
        stamp = time.strftime("%Y%m%d%H%M%S")
        base = self.child_db_path(int(row["bot_id"]))
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(base) + suffix)
            if source.exists():
                try:
                    archive.mkdir(parents=True, exist_ok=True)
                    source.rename(archive / f"{source.name}.{stamp}")
                except OSError:
                    pass
        return True

    async def setup_bot(self, app: Application) -> None:
        """命令菜单 + （可选）把欢迎消息同步到机器人简介页。"""
        commands = [
            BotCommand("start", "开始 / 主菜单"),
            BotCommand("search", "搜索关键词"),
            BotCommand("my", "我的提交"),
            BotCommand("help", "帮助"),
            BotCommand("id", "查看我的ID"),
            BotCommand("cancel", "取消当前操作"),
        ]
        if app.bot_data.get("is_mother"):
            commands.insert(2, BotCommand("clone", "克隆机器人"))
        try:
            await app.bot.set_my_commands(commands)
        except TelegramError:
            logging.info("set_my_commands failed for %s", app.bot_data.get("bot_id"))
        await self.apply_description(app.bot)

    async def apply_description(self, bot, force: bool = False) -> None:
        """把欢迎消息文字写到机器人简介页；没设置欢迎消息时不动原简介（force=清除时清空）。"""
        if self.store.get_setting("welcome_sync_description", "1") != "1":
            return
        content = self.get_json("welcome_content")
        text = str((content or {}).get("text") or "").strip()
        if not text and not force:
            return
        try:
            await bot.set_my_description(text[:512])
            await bot.set_my_short_description(text[:120])
        except TelegramError:
            logging.info("set description failed")

    async def apply_description_all(self, force: bool = False) -> int:
        count = 0
        for app in list(self.apps.values()):
            await self.apply_description(app.bot, force=force)
            count += 1
        return count

    async def start_all(self) -> None:
        await self.start_mother()
        for row in self.store.enabled_children():
            await self.start_child(row)

    async def stop_all(self) -> None:
        for bot_id in list(self.apps):
            app = self.apps.pop(bot_id)
            await self._shutdown(app)
        self.mother_app = None

    async def run(self) -> None:
        import signal

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await self.start_all()
        logging.info("running: mother %s + %d child bots", self.mother_bot_id, len(self.apps) - 1)
        try:
            await stop.wait()
        finally:
            await self.stop_all()
