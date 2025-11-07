# XL Reminder Bot

Bot Telegram untuk memantau paket data XL, masa aktif kartu, dan mengirimkan pengingat sebelum paket habis. Proyek ini menggunakan Python dan [python-telegram-bot 20](https://docs.python-telegram-bot.org/en/v20.6/) dengan scheduler dari APScheduler.

## Fitur utama
- Menyimpan banyak nomor XL dengan label berbeda di SQLite.
- Menampilkan semua paket aktif beserta kuota dan masa berlaku lengkap.
- Pengingat otomatis H-1 **dan** Hari-H yang dapat diaktif/nonaktifkan per pengguna.
- Pengaturan jam pengiriman reminder per pengguna langsung dari bot.
- Menu pengelolaan nomor (tambah, edit, hapus, cari, sortir).
- Export ICS untuk sinkronisasi kalender.
- Backup & restore database (khusus admin).

## Prasyarat
- Python 3.10 atau yang lebih baru.
- Token bot Telegram dari [BotFather](https://core.telegram.org/bots#6-botfather).
- Koneksi internet untuk memanggil API pengecekan paket.

## Instalasi cepat

Gunakan skrip `bot_xlremainder.sh` untuk otomatis menyiapkan lingkungan virtual dan dependensi.

```bash
./bot_xlremainder.sh
```

Skrip akan:
1. Membuat virtual environment `.venv` di folder proyek bila belum ada.
2. Memperbarui `pip` dan memasang dependensi dari `requirements.txt`.
3. Membuat berkas `.env` contoh jika belum tersedia.

> Jika skrip belum executable jalankan `chmod +x bot_xlremainder.sh` terlebih dahulu.

## Konfigurasi

Salin atau sunting berkas `.env` untuk menyesuaikan variabel berikut:

```env
BOT_TOKEN=123456789:ABCDEF        # wajib, token bot Telegram
API_TEMPLATE=https://...          # endpoint API pengecekan paket
DB_PATH=./xl_reminder.db          # lokasi database SQLite
REQUEST_TIMEOUT=12                # detik timeout request API
REFRESH_INTERVAL_SECS=21600       # interval refresh otomatis (detik)
REMINDER_HOUR=9                   # jam default reminder (WIB)
ADMIN_IDS=12345,67890             # optional, ID Telegram yang boleh backup/restore
BACKUP_DIR=./backups              # folder backup database
WEEKLY_BACKUP_DAY=sun             # hari backup mingguan (mon..sun)
WEEKLY_BACKUP_HOUR=2              # jam backup mingguan (WIB)
```

Setiap pengguna bisa mengubah jam reminder dari menu `⚙️ Pengaturan` tanpa mengubah `.env`.

## Menjalankan bot

Aktifkan virtual environment kemudian jalankan skrip utama:

```bash
source .venv/bin/activate
python xl_bot.py
```

Bot akan menjalankan:
- Refresh cache paket otomatis setiap 6 jam (scan tiap 30 menit).
- Pengingat H-1 & Hari-H sesuai jam pilihan pengguna.
- Backup mingguan sesuai konfigurasi.

## Pengembangan & testing

- Gunakan `python -m pip install -r requirements.txt` bila tidak memakai skrip otomatis.
- Database tersimpan di `xl_reminder.db`. Gunakan menu Backup/Restore untuk migrasi aman.
- Jalankan bot di mode debug dengan `LOG_LEVEL=DEBUG` (set via environment variable) jika diperlukan.

## Lisensi

Proyek ini dibagikan apa adanya tanpa jaminan. Silakan sesuaikan untuk kebutuhan internal Anda.
