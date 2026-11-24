import tempfile
import time
import unittest
from pathlib import Path

from tg_search_bot.runtime import TokenCipher, token_bot_id
from tg_search_bot.storage import HubStore, SearchStore, entry_keyword_key


class KeywordTest(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(entry_keyword_key("ＶＸ８ 地址"), "vx8")
        self.assertEqual(entry_keyword_key(" V8  "), "v8")
        self.assertEqual(entry_keyword_key("v8地址"), entry_keyword_key("V8"))
        self.assertEqual(entry_keyword_key("地址"), "地址")
        self.assertEqual(entry_keyword_key("v\u200b8"), "v8")


class SearchStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SearchStore(Path(self.temp.name) / "s.sqlite3")
        self.store.init()
        self.n = 0

    def tearDown(self):
        self.temp.cleanup()

    def add(self, kw, text, status="pending"):
        self.n += 1
        return self.store.add_rich_submission(kw, f"tgcontent://1/{self.n}", text, 7, "u", status=status)

    def test_exact_match_and_suffix(self):
        self.add("v8地址", "A", "approved")
        self.assertEqual(self.store.find_keyword_entry("V8").content_text, "A")
        self.assertEqual(self.store.find_keyword_entry("v8 地址").content_text, "A")
        self.assertIsNone(self.store.find_keyword_entry("v8p"))
        self.assertIsNone(self.store.find_keyword_entry("请问v8地址在哪"))

    def test_same_keyword_last_one_wins(self):
        first = self.add("v8", "old", "approved")
        second = self.add("V8地址", "new")
        self.assertEqual(self.store.find_keyword_entry("v8").content_text, "old")
        self.assertTrue(self.store.review(second, True, 1))
        self.assertEqual(self.store.find_keyword_entry("v8").content_text, "new")
        self.assertIsNone(self.store.get(first))
        self.assertFalse(self.store.review(second, True, 1))  # 不能重复审核

    def test_pending_not_public_and_reject(self):
        entry = self.add("abc", "x")
        self.assertIsNone(self.store.find_keyword_entry("abc"))
        self.assertTrue(self.store.review(entry, False, 1))
        self.assertIsNone(self.store.find_keyword_entry("abc"))

    def test_broad_search(self):
        for i in range(15):
            self.add(f"钱包{i}", "x", "approved")
        self.assertEqual(len(self.store.search_keyword_titles("钱包")), 10)
        self.assertEqual(self.store.search_keyword_titles("zzz"), [])

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            self.add("  ", "x")
        with self.assertRaises(ValueError):
            self.add("kw", "   ")

    def test_users_and_counts(self):
        self.store.touch_user(5, "a")
        self.store.touch_user(6, "b")
        self.store.mark_user_blocked(6)
        self.assertEqual(self.store.active_user_ids(), [5])
        self.store.touch_user(6, "b")  # 重新私聊即恢复
        self.assertEqual(self.store.active_user_ids(), [5, 6])
        self.add("k", "x", "approved")
        self.assertEqual(self.store.counts(), {"approved": 1, "pending": 0})
        self.assertGreater(self.store.storage_usage_bytes(), 0)


class HubStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = HubStore(Path(self.temp.name) / "hub.sqlite3")
        self.hub.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_child_registry(self):
        cid = self.hub.save_child_request(222, "c", "Child", 5, "Owner", "cipher")
        self.assertEqual(self.hub.child(cid)["status"], "pending")
        self.assertTrue(self.hub.review_child(cid, True, 1))
        self.assertFalse(self.hub.review_child(cid, True, 1))
        self.assertEqual(len(self.hub.enabled_children()), 1)
        # 重新提交同一个机器人 → 重新待审核，不重复登记
        again = self.hub.save_child_request(222, "c", "Child", 6, "New", "cipher2")
        self.assertEqual(again, cid)
        self.assertEqual(self.hub.child(cid)["status"], "pending")
        self.assertEqual(self.hub.child(cid)["owner_id"], 6)
        self.hub.set_child_enabled(cid, False)
        self.assertEqual(self.hub.enabled_children(), [])
        self.assertTrue(self.hub.delete_child(cid))

    def test_relay_and_deletions_and_cache(self):
        self.hub.map_relay("receiver", 1, 300, 50, 222, 400, 9)
        row = self.hub.relay(1, 300, 50)
        self.assertEqual((row["bot_id"], row["user_id"], row["peer_message_id"]), (222, 400, 9))
        self.hub.forget_relay(1, 300, 50)
        self.assertIsNone(self.hub.relay(1, 300, 50))
        self.hub.schedule_deletion(1, 300, 50, 10)
        self.hub.schedule_deletion(1, 300, 51, 0)  # 0 = 不删除
        self.assertEqual(self.hub.due_deletions(), [])
        due = self.hub.due_deletions(now=time.time() + 11)
        self.assertEqual(len(due), 1)
        self.hub.finish_deletion(int(due[0]["id"]))
        self.assertEqual(self.hub.due_deletions(now=time.time() + 11), [])
        self.hub.cache_file_id(222, "k", "F")
        self.assertEqual(self.hub.cached_file_id(222, "k"), "F")
        self.assertEqual(self.hub.cached_file_id(333, "k"), "")

    def test_developers(self):
        self.hub.add_developer(9, 1)
        self.hub.add_developer(9, 1)
        self.assertEqual(self.hub.extra_developers(), [9])
        self.assertTrue(self.hub.remove_developer(9))


class CipherTest(unittest.TestCase):
    def test_round_trip_and_tamper(self):
        cipher = TokenCipher(b"k" * 32)
        value = cipher.encrypt("123456:ABC")
        self.assertNotIn("ABC", value)
        self.assertEqual(cipher.decrypt(value), "123456:ABC")
        with self.assertRaises(ValueError):
            TokenCipher(b"x" * 32).decrypt(value)

    def test_token_bot_id(self):
        self.assertEqual(token_bot_id("123:abc"), 123)
        self.assertEqual(token_bot_id("bad"), 0)
