#!/usr/bin/env bash
# 在 $APP_DIR 里安装依赖并写入 systemd 服务（install.sh / update-from-git.sh 调用）。
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tg-search-bot}"
APP_USER="${APP_USER:-tgsearch}"
SERVICE_NAME="${SERVICE_NAME:-tg-search-bot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$APP_DIR"

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR/data" "$APP_DIR/backups"

umask 022
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

cat >"/etc/systemd/system/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=TG Search Bot (mother + child bots)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=TZ=Asia/Shanghai
Environment=DATA_DIR=$APP_DIR/data
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py
Restart=always
RestartSec=10
User=$APP_USER
Group=$APP_USER
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data $APP_DIR/backups

[Install]
WantedBy=multi-user.target
UNIT

chown -R root:root "$APP_DIR"
# 服务用 $APP_USER 运行：程序和 .venv 必须对它可读、目录可进入、可执行文件保持可执行
# （服务器 umask 为 027 时 venv 会是 750/640，导致 203/EXEC Permission denied）
chmod -R u+rwX,go+rX,go-w "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data" "$APP_DIR/backups"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"
