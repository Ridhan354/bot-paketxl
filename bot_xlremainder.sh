#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/bot-xlreminder"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/.venv"
SERVICE_NAME="bot-xlreminder"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
REPO_URL="https://github.com/Ridhan354/bot-paketxl.git"
REPO_BRANCH="main"
PYTHON_BIN="${PYTHON:-python3}"
GIT_BIN="${GIT:-git}"
ENV_FILE="$INSTALL_DIR/.env"

require_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Skrip ini harus dijalankan sebagai root (gunakan sudo)." >&2
    exit 1
  fi
}

need_systemctl() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl tidak ditemukan. Pastikan berjalan di sistem dengan systemd." >&2
    exit 1
  fi
}

require_git() {
  if ! command -v "$GIT_BIN" >/dev/null 2>&1; then
    echo "git tidak ditemukan. Pasang git terlebih dahulu." >&2
    exit 1
  fi
}

prompt_default() {
  local prompt="$1" default_value="$2" response
  read -rp "$prompt [$default_value]: " response
  if [ -z "$response" ]; then
    response="$default_value"
  fi
  printf '%s' "$response"
}

update_env_var() {
  local key="$1" value="$2" file="$3"
  local escaped
  escaped=$(printf '%s' "$value" | sed 's/[\\&/]/\\&/g')
  if [ -f "$file" ] && grep -q "^$key=" "$file"; then
    sed -i "s|^$key=.*|$key=$escaped|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

sync_repository() {
  require_git
  install -d -m 755 "$INSTALL_DIR"
  if [ -d "$APP_DIR/.git" ]; then
    echo "Memperbarui repositori di $APP_DIR"
    "$GIT_BIN" -C "$APP_DIR" fetch --all --prune
    "$GIT_BIN" -C "$APP_DIR" reset --hard "origin/$REPO_BRANCH"
  else
    echo "Mengunduh repositori dari $REPO_URL (branch $REPO_BRANCH)"
    rm -rf "$APP_DIR"
    "$GIT_BIN" clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
  fi
}

create_env_file() {
  mkdir -p "$INSTALL_DIR"
  local default_api="https://bendith.my.id/end.php?check=package&number={number}&version=2"
  local default_db="$INSTALL_DIR/xl_reminder.db"
  local default_backup="$INSTALL_DIR/backups"

  if [ -f "$ENV_FILE" ]; then
    echo ".env sudah ada, lewati pembuatan."
    return
  fi

  echo "-- Konfigurasi awal bot --"
  read -rp "Masukkan BOT_TOKEN Telegram: " bot_token
  while [ -z "$bot_token" ]; do
    echo "BOT_TOKEN wajib diisi."
    read -rp "Masukkan BOT_TOKEN Telegram: " bot_token
  done

  local api_template
  api_template=$(prompt_default "Masukkan API template" "$default_api")
  local reminder_hour
  reminder_hour=$(prompt_default "Jam reminder default (0-23)" "9")
  local refresh_interval
  refresh_interval=$(prompt_default "Interval refresh (detik)" "21600")
  local admin_ids
  read -rp "ID Telegram admin (pisahkan dengan koma, boleh kosong): " admin_ids

  cat >"$ENV_FILE" <<EOF_ENV
BOT_TOKEN=$bot_token
API_TEMPLATE=$api_template
DB_PATH=$default_db
REQUEST_TIMEOUT=12
REFRESH_INTERVAL_SECS=$refresh_interval
REMINDER_HOUR=$reminder_hour
ADMIN_IDS=$admin_ids
BACKUP_DIR=$default_backup
WEEKLY_BACKUP_DAY=sun
WEEKLY_BACKUP_HOUR=2
INSTALL_BASE=$INSTALL_DIR
REPO_URL=$REPO_URL
REPO_BRANCH=$REPO_BRANCH
VENV_PATH=$VENV_DIR
EOF_ENV
  echo "Berkas .env dibuat di $ENV_FILE"
}

ensure_env_defaults() {
  update_env_var "INSTALL_BASE" "$INSTALL_DIR" "$ENV_FILE"
  update_env_var "REPO_URL" "$REPO_URL" "$ENV_FILE"
  update_env_var "REPO_BRANCH" "$REPO_BRANCH" "$ENV_FILE"
  update_env_var "VENV_PATH" "$VENV_DIR" "$ENV_FILE"
}

install_dependencies() {
  echo "Sinkronisasi kode dari GitHub"
  sync_repository

  install -d -m 755 "$INSTALL_DIR/backups"

  if [ ! -d "$VENV_DIR" ]; then
    echo "Membuat virtual environment"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  else
    echo "Virtual environment sudah ada"
  fi

  local requirements_file="$APP_DIR/requirements.txt"
  if [ ! -f "$requirements_file" ]; then
    cat >"$requirements_file" <<'EOF_REQ'
python-telegram-bot>=20.6,<21
python-dotenv>=1.0
requests>=2.31
apscheduler>=3.10
pytz>=2023.3
EOF_REQ
    echo "requirements.txt dari repo tidak ditemukan, menggunakan daftar bawaan."
  fi

  echo "Memperbarui pip dan memasang dependensi"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r "$requirements_file"

  touch "$INSTALL_DIR/xl_reminder.db"
  echo "Database disiapkan di $INSTALL_DIR/xl_reminder.db"
}

create_service() {
  need_systemctl
  cat >"$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=XL Reminder Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python $APP_DIR/xl_bot.py
Restart=on-failure
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF_SERVICE
  chmod 644 "$SERVICE_FILE"
  systemctl daemon-reload
  echo "Service systemd dibuat di $SERVICE_FILE"
}

install_bot() {
  require_root
  install_dependencies
  create_env_file
  ensure_env_defaults
  create_service
  echo "Instalasi selesai. Gunakan menu Start untuk menjalankan bot."
}

start_bot() {
  require_root
  need_systemctl
  systemctl start "$SERVICE_NAME"
  echo "Service $SERVICE_NAME dimulai."
}

stop_bot() {
  require_root
  need_systemctl
  systemctl stop "$SERVICE_NAME"
  echo "Service $SERVICE_NAME dihentikan."
}

status_bot() {
  need_systemctl
  systemctl status "$SERVICE_NAME" --no-pager
}

enable_autostart() {
  require_root
  need_systemctl
  systemctl enable "$SERVICE_NAME"
  echo "Service $SERVICE_NAME akan otomatis berjalan saat boot."
}

disable_autostart() {
  require_root
  need_systemctl
  systemctl disable "$SERVICE_NAME"
  echo "Autostart untuk $SERVICE_NAME dinonaktifkan."
}

edit_config() {
  require_root
  if [ ! -f "$ENV_FILE" ]; then
    echo "Berkas $ENV_FILE belum ada. Jalankan menu install terlebih dahulu." >&2
    return
  fi

  local current_token current_admin current_api
  current_token=$(grep '^BOT_TOKEN=' "$ENV_FILE" | cut -d'=' -f2-)
  current_admin=$(grep '^ADMIN_IDS=' "$ENV_FILE" | cut -d'=' -f2-)
  current_api=$(grep '^API_TEMPLATE=' "$ENV_FILE" | cut -d'=' -f2-)

  read -rp "Token bot baru (kosong = tetap) [$current_token]: " new_token
  if [ -n "$new_token" ]; then
    update_env_var "BOT_TOKEN" "$new_token" "$ENV_FILE"
  fi

  read -rp "API template baru (kosong = tetap) [$current_api]: " new_api
  if [ -n "$new_api" ]; then
    update_env_var "API_TEMPLATE" "$new_api" "$ENV_FILE"
  fi

  read -rp "Daftar ID admin baru (pisahkan koma, kosong = tetap) [$current_admin]: " new_admin
  if [ -n "$new_admin" ]; then
    update_env_var "ADMIN_IDS" "$new_admin" "$ENV_FILE"
  fi

  echo "Konfigurasi diperbarui. Restart bot agar perubahan diterapkan."
}

uninstall_bot() {
  require_root
  need_systemctl
  echo "Menghentikan service (jika berjalan)"
  systemctl stop "$SERVICE_NAME" || true
  systemctl disable "$SERVICE_NAME" || true

  if [ -f "$SERVICE_FILE" ]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    echo "Service file dihapus."
  fi

  if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo "Direktori instalasi $INSTALL_DIR dihapus."
  fi

  echo "Uninstall selesai."
}

show_menu() {
  cat <<'MENU'

=== Menu Bot XL Reminder ===
1) Install / Update bot
2) Start bot
3) Stop bot
4) Cek status bot
5) Aktifkan auto start bot
6) Nonaktifkan auto start bot
7) Edit BOT API token / ID admin
8) Uninstall bot
9) Keluar
MENU
}

main() {
  while true; do
    show_menu
    read -rp "Pilih menu [1-9]: " choice
    case "$choice" in
      1) install_bot ;;
      2) start_bot ;;
      3) stop_bot ;;
      4) status_bot ;;
      5) enable_autostart ;;
      6) disable_autostart ;;
      7) edit_config ;;
      8) uninstall_bot ;;
      9) echo "Keluar."; break ;;
      *) echo "Pilihan tidak dikenal." ;;
    esac
  done
}

main "$@"
