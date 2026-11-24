# TG 搜索机器人（母机器人 + 子机器人）

和 `tg-directory-bot` 分开的独立项目，技术栈相同（python-telegram-bot + SQLite），
搜索/收录、克隆、双向的代码从 `tg-directory-bot` 搬过来，方便以后合并接口。

## 一键安装（首次，在服务器上执行）

```
curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-search-bot/main/deploy/ubuntu/install.sh | sudo bash
```

会提示输入：母机器人 Bot Token、开发者 Telegram 数字 ID。Token 只写进服务器 `/opt/tg-search-bot/.env`（权限 600），不会进代码仓库。

## 一键更新

```
curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-search-bot/main/deploy/ubuntu/update-from-git.sh | sudo bash
```

更新前自动备份全部数据库到 `/opt/tg-search-bot/backups/`（保留 10 份）。

## 和现有机器人并排运行

| 项目 | 目录 | 服务名 | 系统用户 | 端口 |
|---|---|---|---|---|
| tg-search-bot | /opt/tg-search-bot | tg-search-bot | tgsearch | 无（不开网页） |
| tg-directory-bot | /opt/tg-directory-bot | tg-directory-bot / -web | tgbot | 8080 |

日志：`sudo journalctl -u tg-search-bot -f`

## 运行方式

一个进程同时跑母机器人和所有子机器人（同一个 asyncio 循环各自轮询），
这样双向消息可以“母机器人转给接收人、原来的子机器人回给用户”。

数据：
- `data/hub.sqlite3`：子机器人登记（Token 加密）、欢迎消息、双向设置、转发映射
- `data/bots/mother.sqlite3`：母机器人的收录
- `data/bots/<机器人ID>.sqlite3`：每个子机器人自己的收录（上限 2GB）
- `data/media/`：欢迎消息/群发的媒体（子机器人首次发送时上传，之后缓存）

## 功能

见 [使用说明.md](使用说明.md)。

## 测试

```
python -m unittest discover -s tests -t .
```
