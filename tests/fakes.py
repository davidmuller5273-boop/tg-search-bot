"""假的 Telegram 接口：每个机器人一个 FakeRequest，记录所有调用。"""
from __future__ import annotations

import itertools
import json
import time

from telegram.request import BaseRequest

CALLS: list = []  # (bot_id, api, params)
_IDS = itertools.count(10000)


class FakeRequest(BaseRequest):
    def __init__(self, bot_id: int, username: str):
        self.bot_id = bot_id
        self.username = username
        self.forbidden_chats: set[int] = set()

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    def _message(self, chat_id, extra=None):
        msg = {
            "message_id": next(_IDS), "date": int(time.time()),
            "chat": {"id": int(chat_id), "type": "private" if int(chat_id) > 0 else "supergroup", "first_name": "x"},
            "from": {"id": self.bot_id, "is_bot": True, "first_name": "Bot", "username": self.username},
        }
        msg.update(extra or {})
        return msg

    async def do_request(self, url, method, request_data=None, **kwargs):
        if "/file/bot" in url:
            return 200, b"FAKEFILEDATA"
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        files = bool(request_data and request_data.contains_files)
        CALLS.append((self.bot_id, api, params, files))
        chat_id = params.get("chat_id")
        if chat_id is not None and int(chat_id) in self.forbidden_chats:
            return 403, json.dumps({"ok": False, "error_code": 403,
                                    "description": "Forbidden: bot was blocked by the user"}).encode()
        if api == "getMe":
            result = {"id": self.bot_id, "is_bot": True, "first_name": f"Bot{self.bot_id}",
                      "username": self.username, "can_join_groups": True,
                      "can_read_all_group_messages": False, "supports_inline_queries": False}
        elif api in {"sendMessage", "editMessageText"}:
            result = self._message(chat_id, {"text": params.get("text", "")})
        elif api == "sendPhoto":
            result = self._message(chat_id, {"photo": [{"file_id": f"photo-{self.bot_id}-{next(_IDS)}",
                                                        "file_unique_id": "u", "width": 1, "height": 1}],
                                             "caption": params.get("caption")})
        elif api in {"sendDocument", "sendVideo", "sendAnimation", "sendAudio", "sendVoice",
                     "sendSticker", "sendVideoNote"}:
            result = self._message(chat_id)
        elif api == "copyMessage":
            result = {"message_id": next(_IDS)}
        elif api == "getFile":
            result = {"file_id": params["file_id"], "file_unique_id": "u", "file_size": 12,
                      "file_path": "photos/x.jpg"}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


REQUESTS: dict[int, FakeRequest] = {}


def fake_app_factory(token: str):
    from telegram.ext import Application

    bot_id = int(token.split(":", 1)[0])
    req = REQUESTS.get(bot_id) or FakeRequest(bot_id, f"bot{bot_id}")
    REQUESTS[bot_id] = req
    return Application.builder().token(token).request(req).get_updates_request(req).build()


def calls(bot_id=None, api=None):
    return [c for c in CALLS if (bot_id is None or c[0] == bot_id) and (api is None or c[1] == api)]
