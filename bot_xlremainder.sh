#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="${PYTHON:-python3}"

printf '\n=== XL Reminder Bot installer ===\n'

if [ ! -d "$VENV_DIR" ]; then
  printf 'Membuat virtual environment di %s...\n' "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  printf 'Virtual environment sudah ada di %s\n' "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

printf 'Memperbarui pip...\n'
pip install --upgrade pip

printf 'Menginstal dependensi dari requirements.txt...\n'
pip install -r "$PROJECT_DIR/requirements.txt"

ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'ENV_TEMPLATE'
# Isi token bot Telegram Anda di sini
BOT_TOKEN=
# API endpoint pengecekan paket (gunakan default bila kosong)
API_TEMPLATE=https://bendith.my.id/end.php?check=package&number={number}&version=2
# Lokasi database SQLite
DB_PATH=./xl_reminder.db
# Pengaturan tambahan (opsional)
REQUEST_TIMEOUT=12
REFRESH_INTERVAL_SECS=21600
REMINDER_HOUR=9
ADMIN_IDS=
BACKUP_DIR=./backups
WEEKLY_BACKUP_DAY=sun
WEEKLY_BACKUP_HOUR=2
ENV_TEMPLATE
  printf 'Berkas .env contoh dibuat di %s\n' "$ENV_FILE"
else
  printf 'Berkas .env sudah ada, tidak diubah.\n'
fi

printf '\nSelesai. Aktifkan lingkungan dengan:\n'
printf '  source %s/bin/activate\n' "$VENV_DIR"
printf 'Lalu jalankan bot:\n'
printf '  python xl_bot.py\n\n'
