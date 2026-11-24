from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_STORAGE_QUOTA = 2 * 1024 * 1024 * 1024  # 每个子机器人收录数据上限 2GB


def parse_ids(value: str | None) -> set[int]:
    ids: set[int] = set()
    for part in str(value or "").replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


@dataclass
class Config:
    bot_token: str
    developer_ids: set[int] = field(default_factory=set)
    data_dir: Path = Path("data")
    secret_key: str = ""
    storage_quota_bytes: int = DEFAULT_STORAGE_QUOTA
    relay_delete_seconds: int = 47 * 3600

    @property
    def cipher_key(self) -> bytes:
        material = self.secret_key or self.bot_token
        return hashlib.sha256(material.encode("utf-8")).digest()


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 BOT_TOKEN（母机器人 Token），请在 .env 中填写")
    data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
    quota = int(os.getenv("STORAGE_QUOTA_BYTES", str(DEFAULT_STORAGE_QUOTA)) or 0)
    return Config(
        bot_token=token,
        developer_ids=parse_ids(os.getenv("DEVELOPER_IDS")),
        data_dir=data_dir,
        secret_key=os.getenv("SECRET_KEY", "").strip(),
        storage_quota_bytes=max(0, quota),
        relay_delete_seconds=max(0, int(os.getenv("RELAY_DELETE_SECONDS", str(47 * 3600)) or 0)),
    )
