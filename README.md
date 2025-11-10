# XL Reminder Bot

Bot Telegram untuk memantau paket data XL, masa aktif kartu, dan mengirimkan pengingat sebelum paket habis. Proyek ini menggunakan Python dan [python-telegram-bot 20](https://docs.python-telegram-bot.org/en/v20.6/) dengan scheduler dari APScheduler.

## Fitur utama

- **Dashboard modern** – daftar nomor tampil rapi dengan status paket, indikator warna masa berlaku, dan ringkasan kuota utama.
- **Pengingat kaya konten** – notifikasi H-1 dan Hari-H otomatis menampilkan seluruh paket yang akan habis dalam format terstruktur.
- **Kelola nomor fleksibel** – tambah, cari, urut, edit, dan hapus nomor langsung dari inline menu Telegram.
- **Sinkron kalender** – ekspor jadwal kedaluwarsa 30 hari ke depan dalam format `.ics` dengan UID stabil.
- **Pemeliharaan otomatis** – refresh berkala, backup mingguan, restore instan, dan update aplikasi via GitHub cukup lewat tombol.
- **Arsitektur modular** – kode dibagi ke paket `bot_paketxl/` (konfigurasi, storage, API client, tampilan, utilitas Telegram) sehingga mudah dikembangkan lagi.

## Struktur kode

```
bot_paketxl/
├── app.py             # kelas utama yang mendaftarkan handler & scheduler
├── api.py             # klien HTTP untuk API pengecekan paket
├── config.py          # pemetaan environment ke AppConfig
├── formatting.py      # utilitas format teks & indikator masa berlaku
├── storage.py         # abstraksi SQLite dan model domain
├── telegram_utils.py  # helper chunking & pengiriman pesan aman
└── views.py           # builder tampilan (overview, detail, reminder)

xl_bot.py              # entry point bot
bot_xlremainder.sh     # skrip instalasi & manajemen service
```

Pendekatan ini memudahkan penambahan fitur baru ataupun pengujian unit tanpa harus berurusan dengan satu skrip monolitik.

## Prasyarat

- Sistem operasi berbasis Linux dengan `systemd`.
- Python 3.10 atau yang lebih baru tersedia di sistem.
- `git`, `python3-venv`, dan `curl`/`wget` (untuk mengunduh repositori) terpasang.
- Token bot Telegram dari [BotFather](https://core.telegram.org/bots#6-botfather).
- Koneksi internet untuk memanggil API pengecekan paket.

## Instalasi cepat

1. Pastikan dependensi sistem tersedia:

   ```bash
   sudo apt update && sudo apt install -y git python3 python3-venv
   ```

2. Ambil repositori lalu jalankan skrip instalasi:

   ```bash
   git clone https://github.com/Ridhan354/bot-paketxl.git
   cd bot-paketxl
   chmod +x bot_xlremainder.sh
   sudo ./bot_xlremainder.sh
   ```

Skrip akan meng-clone versi terbaru langsung dari GitHub ke `/opt/bot-xlreminder/app/`, membuat virtual environment di `/opt/bot-xlreminder/.venv/`, menyiapkan database, dan memasang service `systemd` bernama `bot-xlreminder`.

### Menu skrip instalasi

1. **Install / Update bot** – mengunduh/menyegarkan kode dari GitHub, memperbarui dependensi, memastikan `.env`, dan membuat service.
2. **Start / Stop bot** – mengendalikan service secara langsung.
3. **Cek status bot** – menampilkan status service melalui `systemctl status`.
4. **Aktifkan / Nonaktifkan auto start** – mengatur agar bot otomatis berjalan ketika sistem menyala.
5. **Edit BOT API token / ID admin** – memperbarui nilai `BOT_TOKEN`, `API_TEMPLATE`, atau `ADMIN_IDS` di berkas `.env` tanpa edit manual.
6. **Uninstall bot** – menghentikan service dan menghapus seluruh instalasi dari `/opt/bot-xlreminder/`.

> Menu instalasi akan meminta Anda memasukkan token bot dan beberapa konfigurasi dasar lainnya. Setelah selesai, jalankan menu "Start bot" untuk menyalakan layanan.

## Pembaruan bot

- Jalankan kembali menu **Install / Update bot** pada `bot_xlremainder.sh` untuk menarik commit terbaru dari GitHub dan memperbarui dependensi.
- Admin bot dapat menggunakan tombol **🔁 Update Bot** di dalam Telegram untuk melakukan `git pull` dan pemasangan dependensi tanpa akses server manual.

## Konfigurasi manual

Berkas konfigurasi `.env` tersimpan di `/opt/bot-xlreminder/.env`. Nilai-nilai yang umum disesuaikan antara lain:

```env
BOT_TOKEN=123456789:ABCDEF        # wajib, token bot Telegram
API_TEMPLATE=https://...          # endpoint API pengecekan paket
DB_PATH=/opt/bot-xlreminder/xl_reminder.db
REQUEST_TIMEOUT=12                # detik timeout request API
REFRESH_INTERVAL_SECS=21600       # interval refresh otomatis (detik)
REMINDER_HOUR=9                   # jam default reminder (WIB)
ADMIN_IDS=12345,67890             # optional, ID Telegram yang boleh backup/restore
BACKUP_DIR=/opt/bot-xlreminder/backups
WEEKLY_BACKUP_DAY=sun             # hari backup mingguan (mon..sun)
WEEKLY_BACKUP_HOUR=2              # jam backup mingguan (WIB)
REPO_URL=https://github.com/Ridhan354/bot-paketxl.git
REPO_BRANCH=main
INSTALL_BASE=/opt/bot-xlreminder
VENV_PATH=/opt/bot-xlreminder/.venv
```

Setiap pengguna bisa mengubah jam reminder dari menu `⚙️ Pengaturan` langsung di bot tanpa mengubah `.env`.

## Menjalankan bot secara manual

Jika ingin menjalankan bot tanpa service `systemd`, aktifkan virtual environment yang telah terpasang kemudian jalankan skrip utama:

```bash
source /opt/bot-xlreminder/.venv/bin/activate
python /opt/bot-xlreminder/app/xl_bot.py
```

Bot akan menjalankan:

- Refresh cache paket otomatis setiap 6 jam (scan tiap 30 menit).
- Pengingat H-1 & Hari-H sesuai jam pilihan pengguna.
- Backup mingguan sesuai konfigurasi.

## Pengembangan & testing

- Gunakan `python -m pip install -r requirements.txt` bila ingin bekerja langsung dari sumber sebelum diinstal.
- Database default tersimpan di `/opt/bot-xlreminder/xl_reminder.db`. Gunakan menu Backup/Restore untuk migrasi aman.
- Jalankan bot di mode debug dengan `LOG_LEVEL=DEBUG` (set via environment variable) jika diperlukan.

## Uninstall

Gunakan menu "Uninstall bot" pada skrip `bot_xlremainder.sh` untuk menghentikan service dan menghapus seluruh instalasi dari `/opt/bot-xlreminder/`.

## Lisensi

Proyek ini dibagikan apa adanya tanpa jaminan. Silakan sesuaikan untuk kebutuhan internal Anda.
