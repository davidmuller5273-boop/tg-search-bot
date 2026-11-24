from __future__ import annotations

import asyncio
import logging

from telegram.error import InvalidToken

from tg_search_bot.config import load_config
from tg_search_bot.runtime import Hub


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx 会把带 Token 的完整 URL 打进日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = load_config()
    try:
        asyncio.run(Hub(config).run())
    except InvalidToken:
        # 不把 Token 打进日志
        raise SystemExit("母机器人 BOT_TOKEN 无效：请检查 .env 里的 BOT_TOKEN（或重新运行安装命令）")


if __name__ == "__main__":
    main()
