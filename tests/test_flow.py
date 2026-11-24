"""端到端：母机器人 + 子机器人在同一进程里，用假的 Telegram 接口跑真实的处理流程。"""
import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Update

from tests import fakes
from tg_search_bot import handlers
from tg_search_bot.config import Config
from tg_search_bot.runtime import Hub

MOTHER_ID = 111111
CHILD_ID = 222222
MOTHER_TOKEN = f"{MOTHER_ID}:" + "M" * 35
CHILD_TOKEN = f"{CHILD_ID}:" + "C" * 35
DEV = 100
OWNER = 200
RECEIVER = 300
USER = 400


class FlowBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fakes.CALLS.clear()
        fakes.REQUESTS.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config(bot_token=MOTHER_TOKEN, developer_ids={DEV}, data_dir=Path(self.temp.name),
                        secret_key="s" * 64)
        self.hub = Hub(config)
        self.hub._polling = False
        self.hub.app_factory = fakes.fake_app_factory
        self.hub.fetch_bot_info = AsyncMock(
            return_value=SimpleNamespace(id=CHILD_ID, username=f"bot{CHILD_ID}", first_name="子机器人"))
        await self.hub.start_mother()
        self.addAsyncCleanup(self.hub.stop_all)
        self.update_ids = itertools.count(1)
        self.msg_ids = itertools.count(500)

    def app(self, bot_id):
        return self.hub.apps[bot_id]

    async def send(self, bot_id, who, chat_id=None, reply_to=None, reply_from=None, **content):
        chat_id = chat_id or who
        mid = next(self.msg_ids)
        chat = ({"id": chat_id, "type": "private", "first_name": "U"} if chat_id > 0
                else {"id": chat_id, "type": "supergroup", "title": "G"})
        msg = {"message_id": mid, "date": int(time.time()), "chat": chat,
               "from": {"id": who, "is_bot": False, "first_name": f"U{who}", "username": f"u{who}"}}
        msg.update(content)
        text = content.get("text", "")
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply_to:
            msg["reply_to_message"] = {
                "message_id": reply_to, "date": int(time.time()), "chat": chat,
                "from": reply_from or {"id": bot_id, "is_bot": True, "first_name": "Bot"}, "text": "x",
            }
        app = self.app(bot_id)
        await app.process_update(Update.de_json({"update_id": next(self.update_ids), "message": msg}, app.bot))
        return mid

    async def press(self, bot_id, who, data, message_id=1):
        app = self.app(bot_id)
        payload = {
            "update_id": next(self.update_ids),
            "callback_query": {
                "id": str(next(self.update_ids)), "chat_instance": "c", "data": data,
                "from": {"id": who, "is_bot": False, "first_name": "U"},
                "message": {"message_id": message_id, "date": int(time.time()),
                            "chat": {"id": who, "type": "private", "first_name": "U"},
                            "from": {"id": bot_id, "is_bot": True, "first_name": "Bot"}, "text": "menu"},
            },
        }
        await app.process_update(Update.de_json(payload, app.bot))

    def texts(self, bot_id, chat_id):
        return [c[2].get("text", "") for c in fakes.calls(bot_id, "sendMessage") if int(c[2]["chat_id"]) == chat_id]

    def last_markup(self, bot_id, chat_id, api="sendMessage"):
        for c in reversed(fakes.calls(bot_id, api)):
            if int(c[2]["chat_id"]) == chat_id and c[2].get("reply_markup"):
                markup = c[2]["reply_markup"]
                return markup if isinstance(markup, dict) else json.loads(markup)
        return None

    async def make_child(self):
        await self.send(MOTHER_ID, OWNER, text=CHILD_TOKEN)
        row = self.hub.store.child_by_bot_id(CHILD_ID)
        await self.press(MOTHER_ID, DEV, f"clone:ok:{row['id']}")
        self.assertIn(CHILD_ID, self.hub.apps)
        return row


def keyboard_labels(markup):
    return [b if isinstance(b, str) else b.get("text") for row in markup.get("keyboard", []) for b in row]


def inline_data(markup):
    return [b.get("callback_data") for row in markup.get("inline_keyboard", []) for b in row]


class MenuAndCloneTest(FlowBase):
    async def test_start_menus_by_role(self):
        await self.send(MOTHER_ID, USER, text="/start")
        labels = keyboard_labels(self.last_markup(MOTHER_ID, USER))
        self.assertIn(handlers.MENU_CLONE, labels)
        self.assertNotIn(handlers.MENU_DEV, labels)
        self.assertIn(handlers.DEFAULT_WELCOME, self.texts(MOTHER_ID, USER))
        await self.send(MOTHER_ID, DEV, text="/start")
        labels = keyboard_labels(self.last_markup(MOTHER_ID, DEV))
        self.assertIn(handlers.MENU_DEV, labels)
        self.assertIn(handlers.MENU_CHILDREN, labels)

    async def test_clone_review_start_and_list(self):
        await self.send(MOTHER_ID, OWNER, text="/clone")
        await self.press(MOTHER_ID, OWNER, "clone:new")
        await self.send(MOTHER_ID, OWNER, text=CHILD_TOKEN)
        # Token 消息被删除，Token 加密保存
        self.assertTrue(fakes.calls(MOTHER_ID, "deleteMessage"))
        row = self.hub.store.child_by_bot_id(CHILD_ID)
        self.assertEqual(row["status"], "pending")
        self.assertNotIn("CCCC", row["token_cipher"])
        # 开发者收到审核卡片
        self.assertIn(f"clone:ok:{row['id']}", inline_data(self.last_markup(MOTHER_ID, DEV)))
        self.assertNotIn(CHILD_ID, self.hub.apps)
        await self.press(MOTHER_ID, DEV, f"clone:ok:{row['id']}")
        self.assertIn(CHILD_ID, self.hub.apps)
        self.assertTrue(any("已通过审核" in t for t in self.texts(MOTHER_ID, OWNER)))
        # 子机器人列表：@用户名、名称、ID
        body, _ = handlers.children_view(SimpleNamespace(application=self.hub.mother_app), 0)
        self.assertIn(f"@bot{CHILD_ID}", body)
        self.assertIn(str(CHILD_ID), body)
        self.assertIn("Bot222222", body)  # 启动后从 getMe 更新名称
        # 普通用户看不到全部列表
        await self.press(MOTHER_ID, USER, "clone:all:0")
        self.assertFalse(any("全部子机器人" in t for t in self.texts(MOTHER_ID, USER)))

    async def test_auto_approve_and_cannot_clone_mother(self):
        self.hub.fetch_bot_info.return_value = SimpleNamespace(id=MOTHER_ID, username="m", first_name="M")
        await self.send(MOTHER_ID, OWNER, text=MOTHER_TOKEN)
        self.assertTrue(any("不能克隆母机器人" in t for t in self.texts(MOTHER_ID, OWNER)))
        self.hub.fetch_bot_info.return_value = SimpleNamespace(id=CHILD_ID, username="c", first_name="C")
        await self.press(MOTHER_ID, DEV, "dev:auto")
        await self.send(MOTHER_ID, OWNER, text=CHILD_TOKEN)
        self.assertIn(CHILD_ID, self.hub.apps)

    async def test_child_has_no_clone_or_dev_panel(self):
        await self.make_child()
        await self.send(CHILD_ID, DEV, text="/start")
        labels = keyboard_labels(self.last_markup(CHILD_ID, DEV))
        self.assertNotIn(handlers.MENU_CLONE, labels)
        self.assertNotIn(handlers.MENU_DEV, labels)
        self.assertIn(handlers.MENU_MANAGE, labels)  # 开发者在子机器人上也有管理权限
        await self.send(CHILD_ID, OWNER, text="/start")
        self.assertIn(handlers.MENU_MANAGE, keyboard_labels(self.last_markup(CHILD_ID, OWNER)))
        await self.send(CHILD_ID, USER, text="/start")
        self.assertNotIn(handlers.MENU_MANAGE, keyboard_labels(self.last_markup(CHILD_ID, USER)))
        await self.send(CHILD_ID, USER, text="/clone")
        self.assertIn("克隆功能只在母机器人里提供。", self.texts(CHILD_ID, USER))
        before = len(self.hub.store.children())
        await self.press(CHILD_ID, DEV, "dev:auto")  # 子机器人上没有开发者面板
        self.assertEqual(self.hub.store.get_setting("clone_auto_approve", "0"), "0")
        self.assertEqual(len(self.hub.store.children()), before)

    async def test_disable_enable_delete_child(self):
        row = await self.make_child()
        await self.press(MOTHER_ID, DEV, f"dev:off:{row['id']}")
        self.assertNotIn(CHILD_ID, self.hub.apps)
        self.assertEqual(self.hub.store.child(row["id"])["enabled"], 0)
        await self.press(MOTHER_ID, DEV, f"dev:on:{row['id']}")
        self.assertIn(CHILD_ID, self.hub.apps)
        self.hub.apps[CHILD_ID].bot_data["store"].touch_user(1)
        await self.press(MOTHER_ID, DEV, f"dev:delok:{row['id']}")
        self.assertNotIn(CHILD_ID, self.hub.apps)
        self.assertIsNone(self.hub.store.child(row["id"]))
        self.assertTrue(any((Path(self.temp.name) / "bots" / "deleted").iterdir()))

    async def test_non_developer_cannot_use_dev_panel(self):
        await self.press(MOTHER_ID, USER, "dev:rcvme")
        self.assertEqual(self.hub.store.get_setting("relay_receiver"), "")


class WelcomeTest(FlowBase):
    async def test_no_welcome_keeps_existing_description(self):
        await self.make_child()
        self.assertFalse(fakes.calls(CHILD_ID, "setMyDescription"))
        self.assertTrue(fakes.calls(CHILD_ID, "setMyCommands"))
        await self.press(MOTHER_ID, DEV, "dev:welclr")  # 明确清除才清空简介
        self.assertTrue(fakes.calls(CHILD_ID, "setMyDescription"))

    async def test_welcome_media_shown_on_all_bots(self):
        await self.make_child()
        await self.press(MOTHER_ID, DEV, "dev:welset")
        await self.send(MOTHER_ID, DEV, caption="欢迎来到本群", caption_entities=[
            {"type": "bold", "offset": 0, "length": 2}],
            photo=[{"file_id": "MOTHERPHOTO", "file_unique_id": "u", "width": 1, "height": 1, "file_size": 10}])
        content = self.hub.get_json("welcome_content")
        self.assertEqual(content["media_type"], "photo")
        self.assertTrue(Path(content["media_path"]).is_file())
        # 简介同步到两个机器人
        self.assertTrue(fakes.calls(CHILD_ID, "setMyDescription"))
        # 母机器人直接用原 file_id
        await self.send(MOTHER_ID, USER, text="/start")
        mother_photo = [c for c in fakes.calls(MOTHER_ID, "sendPhoto") if int(c[2]["chat_id"]) == USER][-1]
        self.assertEqual(mother_photo[2]["photo"], "MOTHERPHOTO")
        # 子机器人第一次上传文件，第二次用缓存
        await self.send(CHILD_ID, USER, text="/start")
        first = [c for c in fakes.calls(CHILD_ID, "sendPhoto")][-1]
        self.assertTrue(first[3])  # 上传了文件
        self.assertEqual(first[2]["caption"], "欢迎来到本群")
        self.assertIn("bold", json.dumps(first[2].get("caption_entities")))
        await self.send(CHILD_ID, OWNER, text="/start")
        second = [c for c in fakes.calls(CHILD_ID, "sendPhoto")][-1]
        self.assertFalse(second[3])
        self.assertTrue(str(second[2]["photo"]).startswith(f"photo-{CHILD_ID}-"))

    async def test_welcome_with_buttons_keeps_buttons(self):
        await self.press(MOTHER_ID, DEV, "dev:welset")
        await self.send(MOTHER_ID, DEV, text="点下面", reply_markup={
            "inline_keyboard": [[{"text": "官网", "url": "https://example.com"}]]})
        await self.send(MOTHER_ID, USER, text="/start")
        sent = [c for c in fakes.calls(MOTHER_ID, "sendMessage") if c[2].get("text") == "点下面"][-1]
        self.assertIn("example.com", json.dumps(sent[2]["reply_markup"]) if not isinstance(
            sent[2]["reply_markup"], str) else sent[2]["reply_markup"])
        self.assertIn("👇 请选择功能", self.texts(MOTHER_ID, USER))


class RelayTest(FlowBase):
    async def test_child_user_to_receiver_and_reply_back(self):
        await self.make_child()
        self.hub.store.set_setting("relay_receiver", str(RECEIVER))
        user_mid = await self.send(CHILD_ID, USER, text="你好，在吗")
        to_receiver = [c for c in fakes.calls(MOTHER_ID, "sendMessage") if int(c[2]["chat_id"]) == RECEIVER]
        self.assertEqual(len(to_receiver), 2)  # 说明 + 原消息
        self.assertIn(f"@bot{CHILD_ID}", to_receiver[0][2]["text"])
        self.assertEqual(to_receiver[1][2]["text"], "你好，在吗")
        self.assertIn("消息已送达", " ".join(self.texts(CHILD_ID, USER)))
        relay_rows = self.hub.store._all("SELECT * FROM relay_map WHERE side='receiver'")
        self.assertEqual({r["bot_id"] for r in relay_rows}, {CHILD_ID})
        forwarded_id = relay_rows[-1]["message_id"]
        # 接收人在母机器人里回复
        await self.send(MOTHER_ID, RECEIVER, text="在的，请说", reply_to=forwarded_id, reply_from={
            "id": MOTHER_ID, "is_bot": True, "first_name": "M"})
        back = [c for c in fakes.calls(CHILD_ID, "sendMessage") if c[2].get("text") == "在的，请说"]
        self.assertEqual(len(back), 1)
        self.assertEqual(int(back[0][2]["chat_id"]), USER)
        self.assertIn(str(user_mid), json.dumps(back[0][2].get("reply_parameters")))
        self.assertIn("✅ 已回复", self.texts(MOTHER_ID, RECEIVER))
        # 47 小时自动删除已安排
        due = self.hub.store.due_deletions(now=time.time() + 47 * 3600 + 5)
        self.assertTrue(any(int(r["bot_id"]) == CHILD_ID for r in due))
        self.assertTrue(any(int(r["bot_id"]) == MOTHER_ID for r in due))
        # 用户回复那条回复 → 接收人那边引用原来的对话
        user_side = self.hub.store._all("SELECT * FROM relay_map WHERE side='user'")[-1]
        await self.send(CHILD_ID, USER, text="好的", reply_to=user_side["message_id"])
        header = [c for c in fakes.calls(MOTHER_ID, "sendMessage") if int(c[2]["chat_id"]) == RECEIVER][-2]
        self.assertIsNotNone(header[2].get("reply_parameters"))

    async def test_media_crosses_bots(self):
        await self.make_child()
        self.hub.store.set_setting("relay_receiver", str(RECEIVER))
        await self.send(CHILD_ID, USER, caption="截图", photo=[
            {"file_id": "CHILDFILE", "file_unique_id": "u", "width": 1, "height": 1, "file_size": 10}])
        self.assertTrue(fakes.calls(CHILD_ID, "getFile"))
        photo = fakes.calls(MOTHER_ID, "sendPhoto")[-1]
        self.assertEqual(int(photo[2]["chat_id"]), RECEIVER)
        self.assertTrue(photo[3])
        self.assertEqual(photo[2]["caption"], "截图")
        # 接收人回复图片 → 子机器人上传给用户
        forwarded = self.hub.store._all("SELECT * FROM relay_map WHERE side='receiver'")[-1]
        await self.send(MOTHER_ID, RECEIVER, reply_to=forwarded["message_id"], photo=[
            {"file_id": "MOTHERFILE", "file_unique_id": "u", "width": 1, "height": 1, "file_size": 10}])
        back = fakes.calls(CHILD_ID, "sendPhoto")[-1]
        self.assertEqual(int(back[2]["chat_id"]), USER)

    async def test_mother_user_relay_uses_copy(self):
        self.hub.store.set_setting("relay_receiver", str(RECEIVER))
        await self.send(MOTHER_ID, USER, text="母机器人的用户")
        self.assertTrue([c for c in fakes.calls(MOTHER_ID, "copyMessage") if int(c[2]["chat_id"]) == RECEIVER])

    async def test_disabled_child_reply_fails_gracefully(self):
        row = await self.make_child()
        self.hub.store.set_setting("relay_receiver", str(RECEIVER))
        await self.send(CHILD_ID, USER, text="hi")
        forwarded = self.hub.store._all("SELECT * FROM relay_map WHERE side='receiver'")[-1]
        await self.press(MOTHER_ID, DEV, f"dev:off:{row['id']}")
        await self.send(MOTHER_ID, RECEIVER, text="回复", reply_to=forwarded["message_id"])
        self.assertTrue(any("已停用" in t for t in self.texts(MOTHER_ID, RECEIVER)))

    async def test_no_receiver_gives_hint_and_receiver_not_self_forwarded(self):
        await self.send(MOTHER_ID, USER, text="随便说说")
        self.assertTrue(any("没有找到该关键词" in t for t in self.texts(MOTHER_ID, USER)))
        self.hub.store.set_setting("relay_receiver", str(RECEIVER))
        await self.send(MOTHER_ID, RECEIVER, text="我自己说话")
        self.assertFalse([c for c in fakes.calls(MOTHER_ID, "copyMessage") if int(c[2]["chat_id"]) == RECEIVER])

    async def test_set_receiver_by_dev(self):
        await self.press(MOTHER_ID, DEV, "dev:rcv")
        await self.send(MOTHER_ID, DEV, text=str(RECEIVER))
        self.assertEqual(self.hub.store.get_setting("relay_receiver"), str(RECEIVER))
        await self.press(MOTHER_ID, DEV, "dev:relaytog")
        self.assertEqual(self.hub.store.get_setting("relay_enabled"), "0")

    async def test_scheduled_deletions_use_right_bot(self):
        await self.make_child()
        self.hub.store.schedule_deletion(CHILD_ID, USER, 77, 1)
        self.hub.store._run("UPDATE scheduled_deletions SET due_at=0")
        await handlers.process_deletions(SimpleNamespace(application=self.hub.mother_app))
        self.assertTrue([c for c in fakes.calls(CHILD_ID, "deleteMessage") if int(c[2]["message_id"]) == 77])
        self.assertEqual(self.hub.store.due_deletions(), [])


class SearchFlowTest(FlowBase):
    async def test_child_search_independent(self):
        await self.make_child()
        # 子机器人超级管理员提交直接生效
        await self.send(CHILD_ID, OWNER, text="v8 搜录 https://v8.example 官方地址", entities=[
            {"type": "bold", "offset": 6, "length": 2}])
        await self.send(CHILD_ID, USER, text="V8地址")
        self.assertIn("https://v8.example 官方地址", self.texts(CHILD_ID, USER))
        # 母机器人的数据是独立的
        await self.send(MOTHER_ID, USER, text="v8地址")
        self.assertIn("没有收录该关键词，请联系管理员。", self.texts(MOTHER_ID, USER))

    async def test_user_submission_review(self):
        await self.make_child()
        await self.send(CHILD_ID, USER, text="钱包 搜录 新内容")
        entry = self.app(CHILD_ID).bot_data["store"].pending_entries()[0]
        self.assertIn(f"rv:a:{entry.id}", inline_data(self.last_markup(CHILD_ID, OWNER)))
        await self.press(CHILD_ID, USER, f"rv:a:{entry.id}")  # 普通用户不能审核
        self.assertEqual(self.app(CHILD_ID).bot_data["store"].get(entry.id).status, "pending")
        await self.press(CHILD_ID, OWNER, f"rv:a:{entry.id}")
        self.assertEqual(self.app(CHILD_ID).bot_data["store"].get(entry.id).status, "approved")
        await self.send(CHILD_ID, 555, text="钱包")
        self.assertIn("新内容", self.texts(CHILD_ID, 555))

    async def test_reply_form_copies_original(self):
        await self.send(MOTHER_ID, DEV, text="公告 搜录", reply_to=42, reply_from={
            "id": DEV, "is_bot": False, "first_name": "D"})
        await self.send(MOTHER_ID, USER, text="公告")
        copied = [c for c in fakes.calls(MOTHER_ID, "copyMessage") if int(c[2]["chat_id"]) == USER]
        self.assertEqual(int(copied[-1][2]["message_id"]), 42)

    async def test_group_precise_trigger(self):
        await self.send(MOTHER_ID, DEV, text="v8地址 搜录 V8内容")
        group = -100500
        await self.send(MOTHER_ID, USER, chat_id=group, text="v8")
        self.assertIn("V8内容", self.texts(MOTHER_ID, group))
        n = len(self.texts(MOTHER_ID, group))
        await self.send(MOTHER_ID, USER, chat_id=group, text="请问一下大家有没有人知道那个最新的钱包下载地址")
        await self.send(MOTHER_ID, USER, chat_id=group, text="v8p")
        self.assertEqual(len(self.texts(MOTHER_ID, group)), n)
        await self.send(MOTHER_ID, USER, chat_id=group, text="v7地址")
        self.assertIn("没有收录该关键词，请联系管理员。", self.texts(MOTHER_ID, group))

    async def test_search_command_lists(self):
        for i in range(3):
            await self.send(MOTHER_ID, DEV, text=f"钱包{i} 搜录 内容{i}")
        await self.send(MOTHER_ID, USER, text="/search 钱包")
        self.assertTrue(any("找到以下关键词" in t for t in self.texts(MOTHER_ID, USER)))

    async def test_quota_blocks_submission(self):
        await self.make_child()
        self.app(CHILD_ID).bot_data["quota"] = 1
        await self.send(CHILD_ID, OWNER, text="k 搜录 x")
        self.assertTrue(any("存储空间已满" in t for t in self.texts(CHILD_ID, OWNER)))


class BroadcastTest(FlowBase):
    async def test_broadcast_all_bots(self):
        await self.make_child()
        await self.send(MOTHER_ID, USER, text="/start")
        await self.send(CHILD_ID, 555, text="/start")
        fakes.REQUESTS[CHILD_ID].forbidden_chats.add(OWNER)
        self.app(CHILD_ID).bot_data["store"].touch_user(OWNER)
        await self.press(MOTHER_ID, DEV, "dev:bc:all")
        await self.send(MOTHER_ID, DEV, text="系统通知")
        job = self.app(MOTHER_ID).user_data[DEV]["broadcast"]
        result = await handlers.run_broadcast(self.hub, job["content"], "all", DEV, self.hub.mother_bot())
        self.assertEqual(result["blocked"], 1)
        self.assertGreaterEqual(result["sent"], 3)  # 母：USER、DEV；子：555
        self.assertIn("系统通知", self.texts(CHILD_ID, 555))
        self.assertNotIn(OWNER, self.app(CHILD_ID).bot_data["store"].active_user_ids())
