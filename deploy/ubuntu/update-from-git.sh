#!/usr/bin/env bash
# 一键更新：curl -fsSL https://raw.githubusercontent.com/davidmuller5273-boop/tg-search-bot/main/deploy/ubuntu/update-from-git.sh | sudo bash
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
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "没有找到 $APP_DIR/.env，请先运行安装命令。"
  exit 1
fi
command -v git >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq git >/dev/null; }
command -v sqlite3 >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq sqlite3 >/dev/null; }

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

# 备份所有数据库（保留最近 10 份）
stamp="$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)"
backup="$APP_DIR/backups/$stamp"
mkdir -p "$backup"
while IFS= read -r -d '' db; do
  rel="${db#"$APP_DIR"/data/}"
  mkdir -p "$backup/$(dirname "$rel")"
  sqlite3 "$db" ".backup '$backup/$rel'" || cp -a "$db" "$backup/$rel"
done < <(find "$APP_DIR/data" -name '*.sqlite3' -not -path '*/deleted/*' -print0 2>/dev/null)
ls -1dt "$APP_DIR"/backups/*/ 2>/dev/null | tail -n +11 | xargs -r rm -rf
echo "已备份数据库到 $backup"

systemctl stop "$SERVICE_NAME" || true
trap 'echo "更新失败，尝试启动旧服务"; systemctl start "$SERVICE_NAME" || true' ERR
fetch_code
APP_DIR="$APP_DIR" APP_USER="$APP_USER" SERVICE_NAME="$SERVICE_NAME" bash "$APP_DIR/deploy/ubuntu/setup.sh"
trap - ERR

sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "✅ 更新完成，$SERVICE_NAME 正在运行。"
else
  echo "⚠️ 服务没有正常运行，最近日志："
  journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
fi
echo "查看日志：sudo journalctl -u $SERVICE_NAME -f"
