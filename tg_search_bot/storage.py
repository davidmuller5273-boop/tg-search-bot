"""SQLite 存储。

* SearchStore：每个机器人一份（母机器人、每个子机器人各自独立）——收录、用户、设置。
* HubStore：母机器人的全局库——子机器人登记、全局欢迎消息、双向设置、转发映射、定时删除。
"""
from __future__ import annotations

import os
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ENTRY_KEYWORD_SUFFIX = "地址"
_INVISIBLE_KEYWORD_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def entry_keyword_key(value: str, suffix: str = ENTRY_KEYWORD_SUFFIX) -> str:
    """收录关键词归一化（与 tg-directory-bot 相同）。

    NFKC（全角→半角）、去掉空白和零宽字符、忽略大小写，再去掉一个结尾的
    「地址」，所以「XX」和「XX地址」是同一个关键词。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_INVISIBLE_KEYWORD_CHARS)
    text = "".join(text.split()).casefold()
    suffix_key = "".join(unicodedata.normalize("NFKC", str(suffix or "")).split()).casefold()
    if suffix_key and len(text) > len(suffix_key) and text.endswith(suffix_key):
        text = text[:-len(suffix_key)]
    return text


def db_size_bytes(path: str | Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(str(path) + suffix)
        except OSError:
            pass
    return total


@dataclass
class Entry:
    id: int
    title: str
    status: str
    user_id: int
    username: str
    content_text: str = ""
    entities_json: str = "[]"
    buttons_json: str = "[]"
    media_file_id: str = ""
    media_type: str = ""
    media_name: str = ""
    copy_chat_id: int = 0
    copy_message_id: int = 0
    created_at: str = ""


class _Base:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self):
        """提交并关闭连接（sqlite3 自带的 with 只提交不关闭）。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.connect() as conn:
            return conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._run(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def storage_usage_bytes(self) -> int:
        return db_size_bytes(self.db_path)


class SearchStore(_Base):
    """一个机器人自己的搜索收录数据。"""

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    keyword_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    content_text TEXT NOT NULL DEFAULT '',
                    entities_json TEXT NOT NULL DEFAULT '[]',
                    buttons_json TEXT NOT NULL DEFAULT '[]',
                    media_file_id TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT '',
                    media_name TEXT NOT NULL DEFAULT '',
                    copy_chat_id INTEGER NOT NULL DEFAULT 0,
                    copy_message_id INTEGER NOT NULL DEFAULT 0,
                    reviewed_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_entries_key ON entries(keyword_key, status, id);
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    blocked INTEGER NOT NULL DEFAULT 0,
                    first_seen REAL NOT NULL DEFAULT 0,
                    last_seen REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    query TEXT NOT NULL DEFAULT '',
                    hits INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0
                );
                """
            )

    # ---- users ----
    def touch_user(self, user_id: int, username: str = "", first_name: str = "", last_name: str = "") -> None:
        now = time.time()
        self._run(
            """INSERT INTO users(user_id, username, first_name, last_name, blocked, first_seen, last_seen)
               VALUES(?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                 first_name=excluded.first_name, last_name=excluded.last_name,
                 blocked=0, last_seen=excluded.last_seen""",
            (int(user_id), username[:80], first_name[:120], last_name[:120], now, now),
        )

    def mark_user_blocked(self, user_id: int) -> None:
        self._run("UPDATE users SET blocked=1 WHERE user_id=?", (int(user_id),))

    def active_user_ids(self) -> list[int]:
        return [int(r["user_id"]) for r in self._all("SELECT user_id FROM users WHERE blocked=0 ORDER BY user_id")]

    def user_count(self) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM users")["n"])

    def record_search(self, user_id: int, query: str, hits: int) -> None:
        self._run(
            "INSERT INTO searches(user_id, query, hits, created_at) VALUES(?, ?, ?, ?)",
            (int(user_id or 0), str(query)[:120], int(hits), time.time()),
        )

    # ---- entries ----
    @staticmethod
    def _entry(row: sqlite3.Row) -> Entry:
        return Entry(
            id=int(row["id"]), title=str(row["title"]), status=str(row["status"]),
            user_id=int(row["user_id"]), username=str(row["username"] or ""),
            content_text=str(row["content_text"] or ""),
            entities_json=str(row["entities_json"] or "[]"),
            buttons_json=str(row["buttons_json"] or "[]"),
            media_file_id=str(row["media_file_id"] or ""),
            media_type=str(row["media_type"] or ""), media_name=str(row["media_name"] or ""),
            copy_chat_id=int(row["copy_chat_id"] or 0),
            copy_message_id=int(row["copy_message_id"] or 0),
            created_at=str(row["created_at"] or ""),
        )

    def add_rich_submission(
        self, keyword: str, unique_url: str, content_text: str, user_id: int,
        username: str, status: str = "pending", file_id: str = "",
        file_type: str = "", file_name: str = "", entities_json: str = "[]",
        buttons_json: str = "[]", copy_chat_id: int = 0, copy_message_id: int = 0,
    ) -> int:
        keyword = " ".join(str(keyword or "").strip().split())
        entities_json = entities_json or "[]"
        if entities_json == "[]":
            content_text = (content_text or "").strip()
        if not keyword or not entry_keyword_key(keyword):
            raise ValueError("收录关键词不能为空")
        if not (content_text or "").strip() and not file_id and not copy_message_id:
            raise ValueError("请提供文字、地址、图片、视频或文件")
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO entries (url, title, keyword_key, status, user_id, username,
                         content_text, entities_json, buttons_json, media_file_id, media_type,
                         media_name, copy_chat_id, copy_message_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        unique_url[:2048], keyword[:120], entry_keyword_key(keyword[:120]), status,
                        int(user_id), (username or "")[:80], (content_text or "")[:4096],
                        entities_json, buttons_json or "[]", file_id[:512], file_type[:40],
                        file_name[:240], int(copy_chat_id or 0), int(copy_message_id or 0),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("这条内容已存在或正在审核中") from exc
            entry_id = int(cursor.lastrowid)
            if status == "approved":
                self._dedupe_entry_keyword(conn, entry_id)
            return entry_id

    @staticmethod
    def _dedupe_entry_keyword(conn: sqlite3.Connection, entry_id: int) -> list[int]:
        """同一关键词以最新通过的一条为准：删掉旧的已审核记录和更早的待审核记录。"""
        row = conn.execute(
            "SELECT keyword_key FROM entries WHERE id=? AND status='approved'", (entry_id,)
        ).fetchone()
        key = str(row["keyword_key"] or "") if row else ""
        if not key:
            return []
        ids = [
            int(item["id"]) for item in conn.execute(
                """SELECT id FROM entries WHERE keyword_key=? AND id<>?
                     AND (status<>'pending' OR id<?)""",
                (key, entry_id, entry_id),
            ).fetchall()
        ]
        for old_id in ids:
            conn.execute("DELETE FROM entries WHERE id=?", (old_id,))
        return ids

    def review(self, entry_id: int, approved: bool, reviewer: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE entries SET status=?, reviewed_by=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='pending'""",
                ("approved" if approved else "rejected", int(reviewer), int(entry_id)),
            )
            if cur.rowcount and approved:
                self._dedupe_entry_keyword(conn, int(entry_id))
            return bool(cur.rowcount)

    def delete_entry(self, entry_id: int) -> bool:
        return bool(self._run("DELETE FROM entries WHERE id=?", (int(entry_id),)).rowcount)

    def get(self, entry_id: int) -> Entry | None:
        row = self._one("SELECT * FROM entries WHERE id=?", (int(entry_id),))
        return self._entry(row) if row else None

    def find_keyword_entry(self, text: str, extra_suffixes: tuple[str, ...] = ()) -> Entry | None:
        """精确匹配：整条消息等于关键词（「XX」「XX地址」都算），只取最新通过的一条。"""
        keys = {entry_keyword_key(text)}
        folded = "".join(unicodedata.normalize("NFKC", str(text or "")).split()).casefold()
        for suffix in extra_suffixes:
            suffix_key = "".join(unicodedata.normalize("NFKC", str(suffix or "")).split()).casefold()
            if suffix_key and len(folded) > len(suffix_key) and folded.endswith(suffix_key):
                keys.add(entry_keyword_key(folded[:-len(suffix_key)]))
        keys.discard("")
        if not keys:
            return None
        marks = ",".join("?" for _ in keys)
        row = self._one(
            f"""SELECT * FROM entries WHERE status='approved' AND keyword_key IN ({marks})
                ORDER BY id DESC LIMIT 1""",
            tuple(keys),
        )
        return self._entry(row) if row else None

    def search_keyword_titles(self, query: str, limit: int = 10) -> list[Entry]:
        """模糊搜索：关键词包含查询词，每个关键词一条，最多 limit 条。"""
        key = entry_keyword_key(query)
        if not key:
            return []
        like = "%" + key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._all(
            """SELECT * FROM entries WHERE id IN (
                   SELECT MAX(id) FROM entries WHERE status='approved'
                      AND keyword_key LIKE ? ESCAPE '\\' GROUP BY keyword_key)
               ORDER BY id DESC LIMIT ?""",
            (like, max(1, min(int(limit), 20))),
        )
        return [self._entry(row) for row in rows]

    def pending_entries(self, limit: int = 20) -> list[Entry]:
        return [self._entry(r) for r in self._all(
            "SELECT * FROM entries WHERE status='pending' ORDER BY id LIMIT ?", (int(limit),))]

    def user_entries(self, user_id: int, limit: int = 20) -> list[Entry]:
        return [self._entry(r) for r in self._all(
            "SELECT * FROM entries WHERE user_id=? ORDER BY id DESC LIMIT ?", (int(user_id), int(limit)))]

    def approved_page(self, offset: int, limit: int = 10) -> list[Entry]:
        return [self._entry(r) for r in self._all(
            "SELECT * FROM entries WHERE status='approved' ORDER BY id DESC LIMIT ? OFFSET ?",
            (int(limit), max(0, int(offset))))]

    def counts(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM entries GROUP BY status")
        data = {str(r["status"]): int(r["n"]) for r in rows}
        return {"approved": data.get("approved", 0), "pending": data.get("pending", 0)}


class HubStore(_Base):
    """母机器人的全局库。"""

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS child_bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id INTEGER NOT NULL UNIQUE,
                    username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    owner_id INTEGER NOT NULL,
                    owner_name TEXT NOT NULL DEFAULT '',
                    token_cipher TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '',
                    reviewed_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS developers (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS relay_map (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    side TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    via_bot_id INTEGER NOT NULL,
                    bot_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    peer_message_id INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_msg
                    ON relay_map(via_bot_id, chat_id, message_id);
                CREATE TABLE IF NOT EXISTS scheduled_deletions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    due_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_deletions_due ON scheduled_deletions(due_at);
                CREATE TABLE IF NOT EXISTS file_cache (
                    bot_id INTEGER NOT NULL,
                    content_key TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    PRIMARY KEY(bot_id, content_key)
                );
                """
            )

    # ---- developers ----
    def extra_developers(self) -> list[int]:
        return [int(r["user_id"]) for r in self._all("SELECT user_id FROM developers ORDER BY created_at")]

    def add_developer(self, user_id: int, added_by: int) -> None:
        self._run("INSERT OR IGNORE INTO developers(user_id, added_by) VALUES(?, ?)", (int(user_id), int(added_by)))

    def remove_developer(self, user_id: int) -> bool:
        return bool(self._run("DELETE FROM developers WHERE user_id=?", (int(user_id),)).rowcount)

    # ---- child bots ----
    def save_child_request(
        self, bot_id: int, username: str, name: str, owner_id: int, owner_name: str,
        token_cipher: str, approved: bool = False,
    ) -> int:
        status = "approved" if approved else "pending"
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM child_bots WHERE bot_id=?", (int(bot_id),)).fetchone()
            if row:
                conn.execute(
                    """UPDATE child_bots SET username=?, name=?, owner_id=?, owner_name=?, token_cipher=?,
                         status=?, enabled=1, error='', reviewed_by=0, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (username, name, int(owner_id), owner_name[:120], token_cipher, status, int(row["id"])),
                )
                return int(row["id"])
            cur = conn.execute(
                """INSERT INTO child_bots(bot_id, username, name, owner_id, owner_name, token_cipher, status)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (int(bot_id), username, name, int(owner_id), owner_name[:120], token_cipher, status),
            )
            return int(cur.lastrowid)

    def child(self, child_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM child_bots WHERE id=?", (int(child_id),))

    def child_by_bot_id(self, bot_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM child_bots WHERE bot_id=?", (int(bot_id),))

    def review_child(self, child_id: int, approved: bool, reviewer: int) -> bool:
        return bool(self._run(
            """UPDATE child_bots SET status=?, reviewed_by=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status='pending'""",
            ("approved" if approved else "rejected", int(reviewer), int(child_id)),
        ).rowcount)

    def set_child_enabled(self, child_id: int, enabled: bool) -> None:
        self._run("UPDATE child_bots SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (1 if enabled else 0, int(child_id)))

    def set_child_error(self, child_id: int, error: str) -> None:
        self._run("UPDATE child_bots SET error=? WHERE id=?", (str(error)[:300], int(child_id)))

    def update_child_profile(self, child_id: int, username: str, name: str) -> None:
        self._run("UPDATE child_bots SET username=?, name=? WHERE id=?", (username, name, int(child_id)))

    def delete_child(self, child_id: int) -> bool:
        return bool(self._run("DELETE FROM child_bots WHERE id=?", (int(child_id),)).rowcount)

    def children(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self._all("SELECT * FROM child_bots WHERE status=? ORDER BY id", (status,))
        return self._all("SELECT * FROM child_bots WHERE status<>'rejected' ORDER BY id")

    def children_of(self, owner_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM child_bots WHERE owner_id=? ORDER BY id", (int(owner_id),))

    def enabled_children(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM child_bots WHERE status='approved' AND enabled=1 ORDER BY id")

    # ---- relay map ----
    def map_relay(self, side: str, via_bot_id: int, chat_id: int, message_id: int,
                  bot_id: int, user_id: int, peer_message_id: int = 0) -> None:
        """side='receiver'：接收人在母机器人里看到的消息 → 来自 bot_id 的用户 user_id。
        side='user'：用户在 bot_id 里收到的回复 → 接收人那边的 peer_message_id。"""
        self._run(
            """INSERT OR REPLACE INTO relay_map(side, chat_id, message_id, via_bot_id, bot_id,
                 user_id, peer_message_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (side, int(chat_id), int(message_id), int(via_bot_id), int(bot_id), int(user_id),
             int(peer_message_id or 0), time.time()),
        )

    def relay(self, via_bot_id: int, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM relay_map WHERE via_bot_id=? AND chat_id=? AND message_id=?",
            (int(via_bot_id), int(chat_id), int(message_id)),
        )

    def forget_relay(self, via_bot_id: int, chat_id: int, message_id: int) -> None:
        self._run("DELETE FROM relay_map WHERE via_bot_id=? AND chat_id=? AND message_id=?",
                  (int(via_bot_id), int(chat_id), int(message_id)))

    # ---- scheduled deletions ----
    def schedule_deletion(self, bot_id: int, chat_id: int, message_id: int, delay: float) -> None:
        if delay <= 0:
            return
        self._run(
            "INSERT INTO scheduled_deletions(bot_id, chat_id, message_id, due_at) VALUES(?, ?, ?, ?)",
            (int(bot_id), int(chat_id), int(message_id), time.time() + float(delay)),
        )

    def due_deletions(self, now: float | None = None, limit: int = 200) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM scheduled_deletions WHERE due_at<=? ORDER BY due_at LIMIT ?",
            (time.time() if now is None else now, int(limit)),
        )

    def finish_deletion(self, deletion_id: int, retry: bool = False) -> None:
        if retry:
            self._run("UPDATE scheduled_deletions SET attempts=attempts+1, due_at=due_at+60 WHERE id=?",
                      (int(deletion_id),))
            self._run("DELETE FROM scheduled_deletions WHERE id=? AND attempts>=3", (int(deletion_id),))
        else:
            self._run("DELETE FROM scheduled_deletions WHERE id=?", (int(deletion_id),))

    # ---- per-bot uploaded file cache (file_id 不能跨机器人使用) ----
    def cached_file_id(self, bot_id: int, content_key: str) -> str:
        row = self._one("SELECT file_id FROM file_cache WHERE bot_id=? AND content_key=?",
                        (int(bot_id), content_key))
        return str(row["file_id"]) if row else ""

    def cache_file_id(self, bot_id: int, content_key: str, file_id: str) -> None:
        self._run("INSERT OR REPLACE INTO file_cache(bot_id, content_key, file_id) VALUES(?, ?, ?)",
                  (int(bot_id), content_key, file_id))
