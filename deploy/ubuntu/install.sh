#!/usr/bin/env bash
# 一键安装（首次）：curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-search-bot/main/deploy/ubuntu/install.sh | sudo bash
# 也可以提前给出：curl ... | sudo BOT_TOKEN=xxx DEVELOPER_IDS=123 bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tg-search-bot}"
APP_USER="${APP_USER:-tgsearch}"
SERVICE_NAME="${SERVICE_NAME:-tg-search-bot}"
REPO_URL="${REPO_URL:-https://github.com/davidmuller5273-boop/tg-search-bot.git}"
BRANCH="${BRANCH:-main}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/davidmuller5273-boop/tg-search-bot/$BRANCH}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 权限运行（命令前加 sudo）"
  exit 1
fi

if [[ "${SKIP_APT:-0}" != "1" ]]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl python3 python3-venv python3-pip sqlite3 openssl >/dev/null
fi

# 拉取最新代码覆盖到 $APP_DIR，保留 .env / data / backups / .venv
fetch_code() {
  local tmp
  tmp="$(mktemp -d)"
  git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$tmp/repo"
  for required in run.py requirements.txt deploy/ubuntu/setup.sh deploy/ubuntu/update-from-git.sh; do
    if [[ ! -f "$tmp/repo/$required" ]]; then
      echo "仓库内容不完整，缺少：$required"
      rm -rf "$tmp"
      exit 1
    fi
  done
  mkdir -p "$APP_DIR"
  find "$APP_DIR" -mindepth 1 -maxdepth 1 \
    ! -name '.env' ! -name '.venv' ! -name 'data' ! -name 'backups' \
    -exec rm -rf -- {} +
  cp -a "$tmp/repo"/. "$APP_DIR"/
  rm -rf "$APP_DIR/.git" "$tmp"
}

ask() {  # ask 变量名 提示
  local __var="$1" __prompt="$2" __value=""
  if [[ -r /dev/tty ]]; then
    read -r -p "$__prompt" __value </dev/tty || true
  fi
  printf -v "$__var" '%s' "$__value"
}

if [[ ! -f "$APP_DIR/.env" ]]; then
  BOT_TOKEN="${BOT_TOKEN:-}"
  DEVELOPER_IDS="${DEVELOPER_IDS:-}"
  while [[ ! "$BOT_TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; do
    ask BOT_TOKEN "请输入【母机器人】Bot Token（@BotFather 获取）: "
    BOT_TOKEN="$(echo "$BOT_TOKEN" | tr -d '[:space:]')"
    if [[ -z "$BOT_TOKEN" && ! -r /dev/tty ]]; then echo "没有终端可输入，请用 BOT_TOKEN=... 方式运行"; exit 1; fi
  done
  ME="$(curl -fsS "https://api.telegram.org/bot$BOT_TOKEN/getMe" 2>/dev/null || true)"
  if [[ "${SKIP_TOKEN_CHECK:-0}" != "1" && "$ME" != *'"ok":true'* ]]; then
    echo "这个 Token 验证失败（Telegram 拒绝），请检查后重新运行。"
    exit 1
  fi
  echo "Token 验证通过：@$(echo "$ME" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
  for other in /opt/tg-directory-bot/.env /opt/safew-bot/.env; do
    if [[ -f "$other" ]] && grep -q "^BOT_TOKEN=$BOT_TOKEN\$" "$other"; then
      echo "这个 Token 已经被 $other 使用，同一个 Token 不能同时跑两个程序，请换一个新机器人。"
      exit 1
    fi
  done
  while [[ ! "$DEVELOPER_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; do
    ask DEVELOPER_IDS "请输入开发者 Telegram 数字ID（多个用英文逗号分隔）: "
    DEVELOPER_IDS="$(echo "$DEVELOPER_IDS" | tr -d '[:space:]' | tr '，' ',')"
    if [[ -z "$DEVELOPER_IDS" && ! -r /dev/tty ]]; then echo "没有终端可输入，请用 DEVELOPER_IDS=... 方式运行"; exit 1; fi
  done
fi

echo "正在下载程序到 $APP_DIR ..."
fetch_code

if [[ ! -f "$APP_DIR/.env" ]]; then
  umask 077
  cat >"$APP_DIR/.env" <<ENV
# TG 搜索机器人配置（不要外传）
BOT_TOKEN=$BOT_TOKEN
DEVELOPER_IDS=$DEVELOPER_IDS
# 加密子机器人 Token 用，改了以后已有子机器人需要重新提交 Token
SECRET_KEY=$(openssl rand -hex 32)
# 子机器人收录数据上限（字节），默认 2GB
STORAGE_QUOTA_BYTES=2147483648
# 双向消息多少秒后自动删除（47 小时）
RELAY_DELETE_SECONDS=169200
ENV
  echo "已生成 $APP_DIR/.env"
fi

APP_DIR="$APP_DIR" APP_USER="$APP_USER" SERVICE_NAME="$SERVICE_NAME" bash "$APP_DIR/deploy/ubuntu/setup.sh"

sleep 3
echo
if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "✅ 安装完成，$SERVICE_NAME 正在运行。去 Telegram 给母机器人发 /start 试试。"
else
  echo "⚠️ 服务没有正常运行，最近日志："
  journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
fi
echo "查看日志：sudo journalctl -u $SERVICE_NAME -f"
echo "以后更新：curl -fsSL $RAW_BASE/deploy/ubuntu/update-from-git.sh | sudo bash"
