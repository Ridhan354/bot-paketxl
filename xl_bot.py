#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, json, sqlite3, logging, requests, html, io, gzip, shutil, asyncio
from typing import Dict, Any, Optional, List, Tuple
from dotenv import load_dotenv

from datetime import datetime, date, timedelta
import pytz  # Asia/Jakarta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# =========================
# ENV & LOGGING
# =========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TEMPLATE = os.getenv("API_TEMPLATE", "https://bendith.my.id/end.php?check=package&number={number}&version=2")
DB_PATH = os.getenv("DB_PATH", "./xl_reminder.db")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12"))
REFRESH_INTERVAL_SECS = int(os.getenv("REFRESH_INTERVAL_SECS", str(6*3600)))  # 6 jam
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))  # WIB jam kirim reminder H-1
# Admin list (koma/space separated telegram user IDs)
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")
WEEKLY_BACKUP_DAY = os.getenv("WEEKLY_BACKUP_DAY", "sun")  # mon..sun
WEEKLY_BACKUP_HOUR = int(os.getenv("WEEKLY_BACKUP_HOUR", "2"))

TZ = pytz.timezone("Asia/Jakarta")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =========================
# CONVERSATION STATES
# =========================
ASK_NUMBER, ASK_LABEL = range(2)
EDIT_PICK, EDIT_LABEL = range(2, 4)
ASK_SEARCH = 4
RESTORE_WAIT_FILE = 5

# =========================
# DB HELPERS + MIGRATION
# =========================
def _db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def _table_exists(con, table: str) -> bool:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None

def _table_columns(con, table: str):
    cur = con.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}

def _add_column_if_missing(con, table: str, col: str, ddl: str):
    cols = _table_columns(con, table)
    if col not in cols:
        logging.info(f"[DB] ALTER TABLE {table} ADD COLUMN {col}")
        con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        con.commit()

def db_init():
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)

    con = _db_conn()
    cur = con.cursor()

    # USERS
    if not _table_exists(con, "users"):
        cur.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL UNIQUE,
            first_name TEXT, last_name TEXT, username TEXT,
            created_at INTEGER NOT NULL
        )""")
        con.commit()
    else:
        cols = _table_columns(con, "users")
        if "id" not in cols:
            cur.execute("ALTER TABLE users RENAME TO users_old")
            cur.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL UNIQUE,
                first_name TEXT, last_name TEXT, username TEXT,
                created_at INTEGER NOT NULL
            )""")
            now_ts = int(time.time())
            old_cols = _table_columns(con, "users_old")
            if "created_at" in old_cols:
                cur.execute("""
                INSERT INTO users (tg_user_id, first_name, last_name, username, created_at)
                SELECT tg_user_id, COALESCE(first_name,''), COALESCE(last_name,''), COALESCE(username,''), created_at
                FROM users_old
                """)
            else:
                cur.execute(f"""
                INSERT INTO users (tg_user_id, first_name, last_name, username, created_at)
                SELECT tg_user_id, COALESCE(first_name,''), COALESCE(last_name,''), COALESCE(username,''), {now_ts}
                FROM users_old
                """)
            cur.execute("DROP TABLE users_old")
            con.commit()

    # NUMBERS
    if not _table_exists(con, "numbers"):
        cur.execute("""
        CREATE TABLE numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            label TEXT,
            msisdn TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0, -- legacy
            created_at INTEGER NOT NULL,
            last_fetch_ts INTEGER DEFAULT 0,
            last_payload TEXT,
            next_retry_ts INTEGER DEFAULT 0,
            last_error TEXT,
            last_notified_expiry TEXT,
            last_notified_type TEXT,
            last_notified_at INTEGER DEFAULT 0
        )""")
        con.commit()
    else:
        _add_column_if_missing(con, "numbers", "last_fetch_ts", "last_fetch_ts INTEGER DEFAULT 0")
        _add_column_if_missing(con, "numbers", "last_payload",  "last_payload TEXT")
        _add_column_if_missing(con, "numbers", "next_retry_ts", "next_retry_ts INTEGER DEFAULT 0")
        _add_column_if_missing(con, "numbers", "last_error",    "last_error TEXT")
        _add_column_if_missing(con, "numbers", "last_notified_expiry", "last_notified_expiry TEXT")
        _add_column_if_missing(con, "numbers", "last_notified_type", "last_notified_type TEXT")
        _add_column_if_missing(con, "numbers", "last_notified_at",     "last_notified_at INTEGER DEFAULT 0")

    # USER_PREFS: sort & search
    if not _table_exists(con, "user_prefs"):
        cur.execute(f"""
        CREATE TABLE user_prefs (
            tg_user_id INTEGER PRIMARY KEY,
            sort_order TEXT NOT NULL DEFAULT 'asc', -- 'asc'/'desc'
            search_query TEXT DEFAULT '',
            reminder_h1 INTEGER NOT NULL DEFAULT 1,
            reminder_h0 INTEGER NOT NULL DEFAULT 1,
            reminder_hour INTEGER NOT NULL DEFAULT {REMINDER_HOUR}
        )""")
        con.commit()
    else:
        _add_column_if_missing(con, "user_prefs", "sort_order",   "sort_order TEXT NOT NULL DEFAULT 'asc'")
        _add_column_if_missing(con, "user_prefs", "search_query", "search_query TEXT DEFAULT ''")
        _add_column_if_missing(con, "user_prefs", "reminder_h1",  "reminder_h1 INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(con, "user_prefs", "reminder_h0",  "reminder_h0 INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(con, "user_prefs", "reminder_hour", f"reminder_hour INTEGER NOT NULL DEFAULT {REMINDER_HOUR}")

    con.close()

# Prefs helpers
def get_prefs(tg_user_id: int) -> Tuple[str, str]:
    con = _db_conn(); cur = con.cursor()
    cur.execute("SELECT sort_order, search_query FROM user_prefs WHERE tg_user_id=?", (tg_user_id,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT INTO user_prefs (tg_user_id, sort_order, search_query) VALUES (?,?,?)",
                    (tg_user_id, "asc", ""))
        con.commit()
        sort_order, search_query = "asc", ""
    else:
        sort_order, search_query = r["sort_order"] or "asc", r["search_query"] or ""
    con.close()
    return sort_order, search_query

def set_sort_order(tg_user_id: int, order: str):
    con = _db_conn(); cur = con.cursor()
    cur.execute("INSERT INTO user_prefs (tg_user_id, sort_order, search_query) VALUES (?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET sort_order=excluded.sort_order",
                (tg_user_id, order, ""))
    con.commit(); con.close()

def set_search_query(tg_user_id: int, query: str):
    con = _db_conn(); cur = con.cursor()
    cur.execute("INSERT INTO user_prefs (tg_user_id, sort_order, search_query) VALUES (?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET search_query=excluded.search_query",
                (tg_user_id, "asc", query))
    con.commit(); con.close()

def clear_search_query(tg_user_id: int):
    con = _db_conn(); cur = con.cursor()
    cur.execute("UPDATE user_prefs SET search_query='' WHERE tg_user_id=?", (tg_user_id,))
    con.commit(); con.close()

def get_reminder_settings(tg_user_id: int) -> Tuple[bool, bool, int]:
    # Pastikan baris pref tersedia
    get_prefs(tg_user_id)
    con = _db_conn(); cur = con.cursor()
    cur.execute("SELECT reminder_h1, reminder_h0, reminder_hour FROM user_prefs WHERE tg_user_id=?", (tg_user_id,))
    row = cur.fetchone(); con.close()
    if not row:
        return True, True, REMINDER_HOUR
    h1 = bool(row["reminder_h1"] if row["reminder_h1"] is not None else 1)
    h0 = bool(row["reminder_h0"] if row["reminder_h0"] is not None else 1)
    hour = row["reminder_hour"] if row["reminder_hour"] is not None else REMINDER_HOUR
    try:
        hour = int(hour)
    except Exception:
        hour = REMINDER_HOUR
    hour = max(0, min(23, hour))
    return h1, h0, hour

def set_reminder_flag(tg_user_id: int, column: str, value: bool):
    if column not in {"reminder_h1", "reminder_h0"}:
        raise ValueError("Invalid reminder flag")
    get_prefs(tg_user_id)
    con = _db_conn(); cur = con.cursor()
    cur.execute(f"UPDATE user_prefs SET {column}=? WHERE tg_user_id=?", (1 if value else 0, tg_user_id))
    con.commit(); con.close()

def toggle_reminder_flag(tg_user_id: int, column: str) -> bool:
    h1, h0, _ = get_reminder_settings(tg_user_id)
    current = {"reminder_h1": h1, "reminder_h0": h0}[column]
    new_val = not current
    set_reminder_flag(tg_user_id, column, new_val)
    return new_val

def set_reminder_hour(tg_user_id: int, hour: int):
    hour = int(hour)
    hour = max(0, min(23, hour))
    get_prefs(tg_user_id)
    con = _db_conn(); cur = con.cursor()
    cur.execute("UPDATE user_prefs SET reminder_hour=? WHERE tg_user_id=?", (hour, tg_user_id))
    con.commit(); con.close()

# Numbers & users helpers
def ensure_user(tg_user) -> None:
    con = _db_conn(); cur = con.cursor()
    cur.execute("SELECT id FROM users WHERE tg_user_id=?", (tg_user.id,))
    if not cur.fetchone():
        cur.execute("""
        INSERT INTO users (tg_user_id, first_name, last_name, username, created_at)
        VALUES (?,?,?,?,?)""", (tg_user.id, tg_user.first_name, tg_user.last_name, tg_user.username, int(time.time())))
        con.commit()
    con.close()

def add_number(tg_user_id: int, label: str, msisdn: str) -> Tuple[bool, str]:
    con = _db_conn(); cur = con.cursor()
    try:
        cur.execute("""
        INSERT INTO numbers (tg_user_id, label, msisdn, is_default, created_at,
                             last_fetch_ts, last_payload, next_retry_ts, last_error,
                             last_notified_expiry, last_notified_type, last_notified_at)
        VALUES (?, ?, ?, 0, ?, 0, NULL, 0, NULL, NULL, NULL, 0)
        """, (tg_user_id, label, msisdn, int(time.time())))
        con.commit()
        return True, "Nomor berhasil didaftarkan."
    except sqlite3.IntegrityError:
        return False, "Nomor sudah terdaftar."
    finally:
        con.close()

def list_numbers(tg_user_id: int) -> List[sqlite3.Row]:
    con = _db_conn(); cur = con.cursor()
    cur.execute("SELECT * FROM numbers WHERE tg_user_id=? ORDER BY created_at ASC", (tg_user_id,))
    rows = cur.fetchall(); con.close()
    return rows

def update_label(tg_user_id: int, msisdn: str, new_label: str) -> int:
    con = _db_conn(); cur = con.cursor()
    cur.execute("UPDATE numbers SET label=? WHERE tg_user_id=? AND msisdn=?", (new_label, tg_user_id, msisdn))
    count = cur.rowcount
    con.commit(); con.close()
    return count

def delete_number(tg_user_id: int, msisdn: str) -> int:
    con = _db_conn(); cur = con.cursor()
    cur.execute("DELETE FROM numbers WHERE tg_user_id=? AND msisdn=?", (tg_user_id, msisdn))
    count = cur.rowcount
    con.commit(); con.close()
    return count

def update_cache(msisdn: str, payload: Optional[Dict[str, Any]], error: Optional[str], block_for_seconds: int = 0):
    con = _db_conn(); cur = con.cursor()
    now = int(time.time())
    next_retry = now + block_for_seconds if block_for_seconds > 0 else 0
    cur.execute("""
        UPDATE numbers
        SET last_fetch_ts=?, last_payload=?, last_error=?, next_retry_ts=?
        WHERE msisdn=?
    """, (now, json.dumps(payload) if payload is not None else None, error, next_retry, msisdn))
    con.commit(); con.close()

def get_cached(msisdn: str) -> Tuple[int, Optional[Dict[str, Any]], Optional[str], int, Optional[str], Optional[str], int]:
    con = _db_conn(); cur = con.cursor()
    cur.execute("""SELECT last_fetch_ts, last_payload, last_error, next_retry_ts,
                          last_notified_expiry, last_notified_type, last_notified_at
                   FROM numbers WHERE msisdn=?""", (msisdn,))
    r = cur.fetchone(); con.close()
    if not r:
        return 0, None, None, 0, None, None, 0
    payload = json.loads(r["last_payload"]) if r["last_payload"] else None
    return (r["last_fetch_ts"] or 0, payload, r["last_error"], r["next_retry_ts"] or 0,
            r["last_notified_expiry"], r["last_notified_type"], r["last_notified_at"] or 0)

def set_last_notified(msisdn: str, expiry_text: str, notif_type: str):
    con = _db_conn(); cur = con.cursor()
    cur.execute("""
        UPDATE numbers SET last_notified_expiry=?, last_notified_type=?, last_notified_at=?
        WHERE msisdn=?
    """, (expiry_text, notif_type, int(time.time()), msisdn))
    con.commit(); con.close()

# =========================
# UTILITIES
# =========================
def normalize_number(raw: str) -> Optional[str]:
    raw = raw.strip()
    raw = re.sub(r"[^\d+]", "", raw)
    if raw.startswith("+"):
        raw = raw[1:]
    if raw.startswith("0"):
        raw = "62" + raw[1:]
    return raw if re.fullmatch(r"62\d{7,14}", raw) else None

def progress_bar(percent: Optional[int], length: int = 20) -> str:
    try:
        p = int(percent)
    except Exception:
        return "—"
    p = max(0, min(100, p))
    filled = int((p / 100.0) * length)
    bar = "█" * filled + "─" * (length - filled)
    return f"<code>[{bar}] {p}%</code>"

def nice_size(s: str) -> str:
    return s if s else "-"

def abbreviate_package(name: str) -> str:
    s = (name or "").lower()
    if "xtra combo" in s and "vip" in s and "youtube" in s:
        return "XCVIP YT"
    caps = "".join(ch for ch in (name or "") if ch.isupper())
    if 2 <= len(caps) <= 8:
        return caps
    parts = re.split(r"\s+", name or "")
    if parts:
        return (parts[0][:8]).upper()
    return "PAKET"

def extract_primary_package(data: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
    pk = (data.get("package_info") or {})
    err = (pk.get("error_message") or "").strip()
    if err:
        return (f"ERROR: {err}", "-", None)
    pkgs = pk.get("packages") or []
    if not pkgs:
        return ("-", "-", None)
    first = pkgs[0]
    full_name = first.get("name", "") or "-"
    abbr = abbreviate_package(full_name)
    expiry = first.get("expiry", "-")
    return (abbr, expiry, full_name)

def parse_expiry_text(expiry_text: str) -> Optional[date]:
    try:
        dt_naive = datetime.strptime(expiry_text, "%d-%m-%Y")
        return dt_naive.date()
    except Exception:
        return None

def indicator_by_date(expiry_text: str) -> Tuple[str, str, Optional[int]]:
    if not expiry_text or expiry_text == "-":
        return "⚪", "unknown", None
    d = parse_expiry_text(expiry_text)
    if not d:
        return "⚪", "unknown", None
    today = datetime.now(TZ).date()
    days = (d - today).days
    if days <= 3:
        return "🔴", "segera", days
    elif days <= 7:
        return "🟡", "waspada", days
    else:
        return "🟢", "aman", days

def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")

# =========================
# MESSAGE RENDER (detail kartu/paket)
# =========================
def build_message(data: Dict[str, Any]) -> str:
    subs = data.get("subs_info", {}) or {}
    pk_info = data.get("package_info", {}) or {}

    msisdn   = subs.get("msisdn", "-")
    operator = subs.get("operator", "-")
    net_type = subs.get("net_type", "-")
    tenure   = subs.get("tenure", "-")
    exp_date = subs.get("exp_date", "-")
    idv      = subs.get("id_verified", "-")

    lines = [
        f"📡 <b>Informasi Kuota — {html.escape(operator)}</b>",
        f"📱 Nomor: <code>{html.escape(msisdn)}</code>",
        f"📶 Jaringan: <b>{html.escape(net_type)}</b>  •  Validasi ID: <b>{html.escape(idv)}</b>",
        f"🕒 Masa Aktif: <b>{html.escape(exp_date)}</b>  •  Tenure: <b>{html.escape(tenure)}</b>",
        ""
    ]

    err_msg = (pk_info.get("error_message") or "").strip()
    if err_msg:
        lines += [
            "🚫 <b>Pengecekan Ditolak</b>",
            f"🧭 Pesan: <i>{html.escape(err_msg)}</i>",
            ""
        ]
    else:
        packages = pk_info.get("packages") or []
        if not packages:
            lines.append("⚠️ Tidak ada paket terdaftar.")
        else:
            if len(packages) > 1:
                lines.append(f"📦 <b>{len(packages)} paket aktif ditemukan:</b>")
            multi = len(packages) > 1
            for idx, pkg in enumerate(packages, start=1):
                lines.append(format_package(pkg, idx if multi else None))
                lines.append("")
            if lines and lines[-1] == "":
                lines.pop()

    lines.append(f"🔔 Dilaporkan: <i>{time.strftime('%Y-%m-%d %H:%M:%S')}</i>")
    return "\n".join(lines)

def format_package(pkg: Dict[str, Any], index: Optional[int] = None) -> str:
    name = pkg.get("name", "Unknown Package")
    expiry = pkg.get("expiry", "-")
    prefix = f"{index}. " if index is not None else ""
    header = f"{prefix}<b>{html.escape(name)}</b>"
    lines = [f"📦 {header}", f"⏳ Kedaluwarsa: <b>{html.escape(expiry)}</b>"]
    for q in (pkg.get("quotas") or []):
        qname = q.get("name", "-")
        bar = progress_bar(q.get("percent"))
        total = nice_size(q.get("total"))
        remaining = nice_size(q.get("remaining"))
        indent = "&nbsp;" * (6 if index is not None else 2)
        lines.append(f"{indent}🔸 <b>{html.escape(qname)}</b>")
        lines.append(f"{indent}&nbsp;&nbsp;{bar} — sisa: <b>{html.escape(remaining)}</b> / {html.escape(total)}")
    return "\n".join(lines)

def reminder_package_lines(packages: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    multi = len(packages) > 1
    for idx, pkg in enumerate(packages, start=1):
        name = (pkg.get("name") or "").strip() or "-"
        expiry = pkg.get("expiry", "-") or "-"
        abbr = abbreviate_package(name)
        prefix = f"{idx}. " if multi else "• "
        lines.append(f"{prefix}<b>{html.escape(abbr)}</b> — {html.escape(expiry)}")
        if name and name.upper() != abbr.upper():
            lines.append(f"&nbsp;&nbsp;{html.escape(name)}")
        quotas = pkg.get("quotas") or []
        if quotas:
            q0 = quotas[0]
            qname = q0.get("name", "-")
            remaining = nice_size(q0.get("remaining"))
            total = nice_size(q0.get("total"))
            lines.append(
                f"&nbsp;&nbsp;Sisa utama: <b>{html.escape(remaining)}</b> / {html.escape(total)} ({html.escape(qname)})"
            )
    return lines

# ===== API & CACHE POLICY =====
LIMIT_PHRASE = "batas maksimal pengecekan"
BLOCK_SECONDS = 3 * 3600  # 3 jam blokir per nomor

def _api_get(number: str) -> Tuple[bool, Optional[Dict[str, Any]], str, int]:
    url = API_TEMPLATE.format(number=number)
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        return False, None, f"Gagal mengambil data: {str(e)}", 0

    if not payload.get("success"):
        msg = payload.get("message") or payload.get("data", {}).get("package_info", {}).get("error_message") or "Unknown error"
        blk = BLOCK_SECONDS if LIMIT_PHRASE in msg.lower() else 0
        return False, payload, msg, blk

    err_msg = (payload.get("data", {}).get("package_info", {}) or {}).get("error_message")
    if err_msg:
        blk = BLOCK_SECONDS if LIMIT_PHRASE in err_msg.lower() else 0
        return False, payload, err_msg, blk

    return True, payload, "", 0

def fetch_and_cache(msisdn: str) -> Tuple[bool, str]:
    ok, payload, err, blk = _api_get(msisdn)
    if ok:
        update_cache(msisdn, payload, None, 0)
        return True, "✅ Data diperbarui."
    else:
        update_cache(msisdn, payload, err, blk)
        return False, f"⚠️ {err}"

def render_from_cache(msisdn: str) -> Optional[str]:
    last_ts, payload, last_error, _, _, _, _ = get_cached(msisdn)
    if payload:
        return build_message(payload.get("data") or {})
    if last_error:
        return f"🚫 <i>{html.escape(last_error)}</i>"
    return None

# =========================
# KEYBOARDS
# =========================
def is_admin(user_id: int) -> bool:
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    return True  # fallback

def main_menu_keyboard(has_numbers: bool, sort_order: str = "asc", has_search: bool = False, user_id: int = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Overview", callback_data="menu_overview")],
        [InlineKeyboardButton("➕ Daftarkan Nomor", callback_data="menu_add")],
    ]
    if has_numbers:
        sort_txt = "↕️ Urut (Sisa Hari) ↑" if sort_order == "asc" else "↕️ Urut (Sisa Hari) ↓"
        search_row = [InlineKeyboardButton("🔎 Cari", callback_data="menu_search")]
        if has_search:
            search_row.append(InlineKeyboardButton("🧹 Hapus Filter", callback_data="menu_search_clear"))
        rows += [
            [InlineKeyboardButton(sort_txt, callback_data="menu_sort_toggle")],
            search_row,
            [InlineKeyboardButton("✏️ Edit Nama", callback_data="menu_edit"),
             InlineKeyboardButton("🗑 Hapus Nomor", callback_data="menu_delete")],
            [InlineKeyboardButton("✅ Cek Sekarang (cache)", callback_data="menu_check"),
             InlineKeyboardButton("🔍 Detail Cepat", callback_data="menu_quick")],
            [InlineKeyboardButton("📅 Export ICS (30 hari)", callback_data="menu_ics")],
        ]
    if user_id is not None and is_admin(user_id):
        rows.append([
            InlineKeyboardButton("🗄 Backup", callback_data="menu_backup_now"),
            InlineKeyboardButton("♻️ Restore", callback_data="menu_restore"),
        ])
    rows.append([InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu_settings")])
    rows.append([InlineKeyboardButton("ℹ️ Bantuan", callback_data="menu_help")])
    return InlineKeyboardMarkup(rows)

def numbers_keyboard(rows: List[sqlite3.Row], action_prefix: str, with_refresh: bool=False) -> InlineKeyboardMarkup:
    buttons, line = [], []
    for r in rows:
        label = (r["label"] or r["msisdn"])
        cb = f"{action_prefix}:{r['msisdn']}"
        line.append(InlineKeyboardButton(label, callback_data=cb))
        if len(line) == 2:
            buttons.append(line); line = []
    if line: buttons.append(line)
    if with_refresh:
        buttons.append([
            InlineKeyboardButton("🔄 Refresh yang due", callback_data=f"{action_prefix}_refresh_due"),
            InlineKeyboardButton("♻️ Paksa semua", callback_data=f"{action_prefix}_refresh_force_all"),
        ])
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(buttons)

# Tombol salin (prefill kolom chat)
def copy_button(msisdn: str) -> InlineKeyboardButton:
    return InlineKeyboardButton("📋 Salin nomor", switch_inline_query_current_chat=msisdn)

# =========================
# OVERVIEW (paket + masa aktif kartu)
# =========================
def build_overview_text(tg_user_id: int) -> str:
    rows = list_numbers(tg_user_id)
    sort_order, search_query = get_prefs(tg_user_id)

    if not rows:
        return ("📊 <b>Overview</b>\n"
                "Belum ada nomor terdaftar. Tekan <b>➕ Daftarkan Nomor</b> untuk menambahkan.\n")

    items = []
    now = int(time.time())
    for r in rows:
        label = r["label"] or "Customer"
        msisdn = r["msisdn"]
        last_ts, payload, last_error, next_retry, _, _, _ = get_cached(msisdn)

        if search_query:
            if (search_query.lower() not in (label.lower())) and (search_query not in msisdn):
                continue

        obj = {
            "label": label, "msisdn": msisdn, "last_ts": last_ts or 0,
            "payload": payload, "last_error": last_error, "next_retry": next_retry or 0
        }

        pkg_days = None
        if payload:
            data = payload.get("data") or {}
            abbr, expiry, _ = extract_primary_package(data)
            if not abbr.startswith("ERROR:") and expiry and expiry != "-":
                d = parse_expiry_text(expiry)
                if d:
                    pkg_days = (d - datetime.now(TZ).date()).days
        obj["pkg_days"] = pkg_days
        items.append(obj)

    def sort_key(x):
        return (x["pkg_days"] is None, x["pkg_days"] if x["pkg_days"] is not None else 10**9, x["label"].lower())
    items.sort(key=sort_key, reverse=(sort_order == "desc"))

    lines = ["📊 <b>Overview</b>"]
    for it in items:
        label, msisdn = it["label"], it["msisdn"]
        last_ts, payload, last_error, next_retry = it["last_ts"], it["payload"], it["last_error"], it["next_retry"]

        if payload:
            data = payload.get("data") or {}
            exp_card = (data.get("subs_info") or {}).get("exp_date", "-")
            card_emo, _, card_days = indicator_by_date(exp_card)
            card_eta = f"{card_days} hari lagi" if (card_days is not None and card_days >= 0) else "lewat jatuh tempo"

            abbr, expiry, _ = extract_primary_package(data)
            if abbr.startswith("ERROR:"):
                lines.append(
                    f"\n⚪ 👤 <b>{html.escape(label)}</b>\n"
                    f"📱 <code>{html.escape(msisdn)}</code>\n"
                    f"💳 {card_emo} Kartu aktif s.d. <b>{html.escape(exp_card)}</b>  •  {card_eta}\n"
                    f"🚫 <i>{html.escape(abbr[6:])}</i>"
                )
            else:
                pkg_emo, _, pkg_days = indicator_by_date(expiry)
                age_min = int((now - (last_ts or now)) / 60)
                eta = f"{pkg_days} hari lagi" if (pkg_days is not None and pkg_days >= 0) else "lewat jatuh tempo"
                lines.append(
                    f"\n👤 <b>{html.escape(label)}</b>\n"
                    f"📱 <code>{html.escape(msisdn)}</code>\n"
                    f"💳 {card_emo} Kartu aktif s.d. <b>{html.escape(exp_card)}</b>  •  {card_eta}\n"
                    f"📦 {pkg_emo} <b>{html.escape(abbr)}</b>  •  ⏳ <b>{html.escape(expiry)}</b>  •  {eta}\n"
                    f"🕘 Cache: {age_min} menit lalu"
                )
        elif last_error:
            wait = max(0, next_retry - now)
            wait_min = int(wait/60)
            lines.append(
                f"\n⚪ 👤 <b>{html.escape(label)}</b>\n"
                f"📱 <code>{html.escape(msisdn)}</code>\n"
                f"🚫  {html.escape(last_error)}\n"
                f"⏳ Coba lagi dalam ~{wait_min} menit"
            )
        else:
            lines.append(
                f"\n⚪ 👤 <b>{html.escape(label)}</b>\n"
                f"📱 <code>{html.escape(msisdn)}</code>\n"
                f"⚠️ Belum ada data. Gunakan <b>✅ Cek Sekarang</b> lalu pilih nomor."
            )

    # (Info indikator & kebijakan dipindahkan ke menu Bantuan)
    return "\n".join(lines)

# =========================
# HANDLERS: BASIC
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    rows = list_numbers(user.id)
    sort_order, search_query = get_prefs(user.id)
    overview = build_overview_text(user.id)
    text = (
        "👋 <b>Selamat datang di XL Reminder Bot</b>\n\n"
        f"{overview}\n"
        "— — —\n"
        "Gunakan tombol di bawah ini 👇"
    )
    kb = main_menu_keyboard(bool(rows), sort_order, bool(search_query), user.id)
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = list_numbers(q.from_user.id)
    sort_order, search_query = get_prefs(q.from_user.id)
    await q.edit_message_text("🏠 <b>Menu Utama</b>", parse_mode=ParseMode.HTML,
                              reply_markup=main_menu_keyboard(bool(rows), sort_order, bool(search_query), q.from_user.id))

async def menu_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = list_numbers(q.from_user.id)
    sort_order, search_query = get_prefs(q.from_user.id)
    await q.edit_message_text("⏳ Membuat overview…")
    overview = build_overview_text(q.from_user.id)
    await q.edit_message_text(overview, parse_mode=ParseMode.HTML,
                              reply_markup=main_menu_keyboard(bool(rows), sort_order, bool(search_query), q.from_user.id))

# Sort toggle
async def menu_sort_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cur, _ = get_prefs(q.from_user.id)
    new_order = "desc" if cur == "asc" else "asc"
    set_sort_order(q.from_user.id, new_order)
    await menu_overview(update, context)

# Search
async def menu_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🔎 Kirim kata kunci untuk <b>mencari</b> (nama atau nomor).", parse_mode=ParseMode.HTML)
    return ASK_SEARCH

async def ask_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = (update.message.text or "").strip()
    set_search_query(update.effective_user.id, term)
    await update.message.reply_text("✅ Filter diterapkan. Menampilkan Overview terfilter…", parse_mode=ParseMode.HTML)
    rows = list_numbers(update.effective_user.id)
    sort_order, search_query = get_prefs(update.effective_user.id)
    overview = build_overview_text(update.effective_user.id)
    await update.message.reply_text(overview, parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu_keyboard(bool(rows), sort_order, bool(search_query), update.effective_user.id))
    return ConversationHandler.END

async def menu_search_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    clear_search_query(q.from_user.id)
    await menu_overview(update, context)

# Add number
async def menu_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("➕ Kirim nomor XL kamu (0819… / +62819… / 62819…).", parse_mode=ParseMode.HTML)
    return ASK_NUMBER

async def ask_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msisdn = normalize_number(update.message.text)
    if not msisdn:
        await update.message.reply_text("Format nomor tidak valid. Contoh: <b>0819xxxxxxx</b>", parse_mode=ParseMode.HTML)
        return ASK_NUMBER
    context.user_data["pending_msisdn"] = msisdn
    await update.message.reply_text(f"Bagus. Beri <b>label/nama</b> untuk <code>{msisdn}</code> (misal: <i>IWAN</i>).", parse_mode=ParseMode.HTML)
    return ASK_LABEL

async def ask_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text.strip()[:32] or "Customer"
    msisdn = context.user_data.get("pending_msisdn")
    ok, msg = add_number(update.effective_user.id, label, msisdn)
    fetched_ok, fetched_msg = fetch_and_cache(msisdn)
    note = "✅ Data awal diambil." if fetched_ok else f"⚠️ {html.escape(fetched_msg)}"
    rows = list_numbers(update.effective_user.id)
    sort_order, search_query = get_prefs(update.effective_user.id)
    await update.message.reply_text(f"{msg}\n{note}\n\nKembali ke menu utama:", parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu_keyboard(True, sort_order, bool(search_query), update.effective_user.id))
    return ConversationHandler.END

# Edit label
async def menu_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = list_numbers(q.from_user.id)
    if not rows:
        sort_order, search_query = get_prefs(q.from_user.id)
        await q.edit_message_text("Belum ada nomor untuk diubah.", reply_markup=main_menu_keyboard(False, sort_order, False, q.from_user.id))
        return ConversationHandler.END
    kb = numbers_keyboard(rows, "edit")
    await q.edit_message_text("Pilih <b>customer</b> yang akan diubah namanya:", parse_mode=ParseMode.HTML, reply_markup=kb)
    return EDIT_PICK

async def edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, msisdn = q.data.split(":", 1)
    context.user_data["edit_msisdn"] = msisdn
    await q.edit_message_text(f"Kirim <b>nama baru</b> untuk <code>{html.escape(msisdn)}</code> (maks 32 karakter).", parse_mode=ParseMode.HTML)
    return EDIT_LABEL

async def edit_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_label = update.message.text.strip()[:32]
    msisdn = context.user_data.get("edit_msisdn")
    if not msisdn or not new_label:
        await update.message.reply_text("Nama tidak valid. Ulangi proses edit.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    count = update_label(update.effective_user.id, msisdn, new_label)
    if count:
        await update.message.reply_text("✅ Nama berhasil diperbarui.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("⚠️ Gagal memperbarui nama.", parse_mode=ParseMode.HTML)
    return ConversationHandler.END

# Delete
async def menu_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = list_numbers(q.from_user.id)
    if not rows:
        sort_order, search_query = get_prefs(q.from_user.id)
        await q.edit_message_text("Belum ada nomor untuk dihapus.", reply_markup=main_menu_keyboard(False, sort_order, bool(search_query), q.from_user.id))
        return
    await q.edit_message_text("Pilih nomor yang akan <b>dihapus</b>:", parse_mode=ParseMode.HTML, reply_markup=numbers_keyboard(rows, "del"))

async def delete_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, msisdn = q.data.split(":", 1)
    count = delete_number(q.from_user.id, msisdn)
    await q.answer("Dihapus." if count else "Tidak ditemukan.")
    rows = list_numbers(q.from_user.id)
    sort_order, search_query = get_prefs(q.from_user.id)
    await q.edit_message_text("Selesai.", parse_mode=ParseMode.HTML,
                              reply_markup=main_menu_keyboard(bool(rows), sort_order, bool(search_query), q.from_user.id))

# Check (cache) + buttons refresh (due/force)
async def menu_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = list_numbers(q.from_user.id)
    if not rows:
        sort_order, search_query = get_prefs(q.from_user.id)
        await q.edit_message_text("Belum ada nomor.", reply_markup=main_menu_keyboard(False, sort_order, bool(search_query), q.from_user.id)); return
    await q.edit_message_text("Pilih nomor untuk lihat detail (cache):", parse_mode=ParseMode.HTML,
                              reply_markup=numbers_keyboard(rows, "chk", with_refresh=True))

async def check_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, msisdn = q.data.split(":", 1)
    cached = render_from_cache(msisdn)
    if not cached:
        await q.edit_message_text("⏳ Ambil data awal…")
        fetch_and_cache(msisdn)
        cached = render_from_cache(msisdn) or "⚠️ Belum ada data."
    # keyboard khusus: Salin + refresh massal + kembali
    kb = InlineKeyboardMarkup([
        [copy_button(msisdn)],
        [InlineKeyboardButton("🔄 Refresh yang due", callback_data="chk_refresh_due"),
         InlineKeyboardButton("♻️ Paksa semua", callback_data="chk_refresh_force_all")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_check")]
    ])
    await q.edit_message_text(cached, parse_mode=ParseMode.HTML, reply_markup=kb)

async def check_refresh_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Merefresh yang sudah due…")
    rows = list_numbers(q.from_user.id)
    now = int(time.time())
    refreshed = 0
    for r in rows:
        msisdn = r["msisdn"]
        last_ts, _, _, next_retry, _, _, _ = get_cached(msisdn)
        if now >= (next_retry or 0) and (now - (last_ts or 0)) >= REFRESH_INTERVAL_SECS:
            fetch_and_cache(msisdn)
            refreshed += 1
    sort_order, search_query = get_prefs(q.from_user.id)
    overview = build_overview_text(q.from_user.id)
    await q.edit_message_text(f"🔄 Selesai. Diresfresh: {refreshed}/{len(rows)} nomor.\n\n{overview}",
                              parse_mode=ParseMode.HTML,
                              reply_markup=main_menu_keyboard(True, sort_order, bool(search_query), q.from_user.id))

async def check_refresh_force_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Memaksa refresh semua…")
    rows = list_numbers(q.from_user.id)
    refreshed = 0
    for r in rows:
        msisdn = r["msisdn"]
        fetch_and_cache(msisdn)  # paksa: abaikan interval & blokir lokal
        refreshed += 1
        await asyncio.sleep(1.5)  # jeda ramah API
    sort_order, search_query = get_prefs(q.from_user.id)
    overview = build_overview_text(q.from_user.id)
    await q.edit_message_text(f"♻️ Selesai (paksa global). Diresfresh: {refreshed}/{len(rows)} nomor.\n\n{overview}",
                              parse_mode=ParseMode.HTML,
                              reply_markup=main_menu_keyboard(True, sort_order, bool(search_query), q.from_user.id))

# QUICK DETAIL ============
async def menu_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = list_numbers(q.from_user.id)
    if not rows:
        sort_order, search_query = get_prefs(q.from_user.id)
        await q.edit_message_text("Belum ada nomor.", reply_markup=main_menu_keyboard(False, sort_order, bool(search_query), q.from_user.id))
        return
    await q.edit_message_text("Pilih nomor untuk <b>Detail Cepat</b>:", parse_mode=ParseMode.HTML,
                              reply_markup=numbers_keyboard(rows, "qdet"))

def render_quick_detail(msisdn: str) -> str:
    last_ts, payload, last_error, _, _, _, _ = get_cached(msisdn)
    if not payload:
        if last_error:
            return f"🚫 {html.escape(last_error)}"
        return "⚠️ Belum ada data pada cache."
    data = payload.get("data") or {}
    subs = data.get("subs_info") or {}
    pk = (data.get("package_info") or {})
    pkgs = pk.get("packages") or []
    full_name = "-"
    expiry = "-"
    quotas = []
    if pkgs:
        pkg0 = pkgs[0]
        full_name = pkg0.get("name", "-") or "-"
        expiry = pkg0.get("expiry", "-") or "-"
        quotas = (pkg0.get("quotas") or [])[:3]  # top 3
    ms = [
        f"👤 <code>{html.escape(subs.get('msisdn','-'))}</code>",
        f"📶 {html.escape(subs.get('net_type','-'))} • ID: {html.escape(subs.get('id_verified','-'))}",
        "",
        f"📦 <b>{html.escape(full_name)}</b>",
        f"⏳ Kedaluwarsa paket: <b>{html.escape(expiry)}</b>"
    ]
    if quotas:
        ms.append("📈 Kuota teratas:")
        for q in quotas:
            qn = q.get("name","-"); pb = progress_bar(q.get("percent"))
            total = q.get("total","-"); rem = q.get("remaining","-")
            ms.append(f"• <b>{html.escape(qn)}</b>\n  {pb} — sisa: <b>{html.escape(rem)}</b> / {html.escape(total)}")
    ms.append(f"\n🕘 Cache: {int((int(time.time()) - (last_ts or int(time.time()))) / 60)} menit lalu")
    return "\n".join(ms)

async def quick_detail_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, msisdn = q.data.split(":", 1)
    txt = render_quick_detail(msisdn)
    kb = InlineKeyboardMarkup([
        [copy_button(msisdn), InlineKeyboardButton("♻️ Paksa nomor ini", callback_data=f"qforce:{msisdn}")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_quick")]
    ])
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)

async def quick_force_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Memaksa refresh nomor ini…")
    _, msisdn = q.data.split(":", 1)
    fetch_and_cache(msisdn)  # paksa
    txt = render_quick_detail(msisdn)
    kb = InlineKeyboardMarkup([
        [copy_button(msisdn), InlineKeyboardButton("♻️ Paksa lagi", callback_data=f"qforce:{msisdn}")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_quick")]
    ])
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)

# =========================
# ICS EXPORT (30 hari)
# =========================
def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

def build_ics_for_user(tg_user_id: int, days_ahead: int = 30) -> bytes:
    rows = list_numbers(tg_user_id)
    today = datetime.now(TZ).date()
    limit = today + timedelta(days=days_ahead)

    events = []
    for r in rows:
        label = r["label"] or "Customer"
        msisdn = r["msisdn"]
        _, payload, _, _, _, _, _ = get_cached(msisdn)
        if not payload:
            continue
        data = payload.get("data") or {}
        abbr, expiry, full_name = extract_primary_package(data)
        if abbr.startswith("ERROR:") or not expiry or expiry == "-":
            continue
        d = parse_expiry_text(expiry)
        if not d or d > limit:
            continue

        title = f"{label} {abbr}".strip()
        uid = f"{msisdn}-{d.strftime('%Y%m%d')}-{abbr.replace(' ','')}-xlreminder"
        dtstamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
        dtstart = yyyymmdd(d)
        dtend = yyyymmdd(d + timedelta(days=1))
        desc = f"Nomor: {msisdn}\\nPaket: {_ics_escape(full_name or abbr)}\\nDibuat oleh XL Reminder Bot"

        ev = "\r\n".join([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"SUMMARY:{_ics_escape(title)}",
            "TRANSP:OPAQUE",
            "CLASS:PUBLIC",
            "STATUS:CONFIRMED",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT"
        ])
        events.append(ev)

    comp = "\r\n".join([
        "BEGIN:VCALENDAR",
        "PRODID:-//XL-Reminder//ID",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        *events,
        "END:VCALENDAR",
        ""
    ])
    return comp.encode("utf-8")

async def menu_ics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = build_ics_for_user(q.from_user.id, days_ahead=30)
    fname = f"xl-expiry-{datetime.now(TZ).strftime('%Y%m%d')}.ics"
    await q.message.reply_document(document=InputFile(io.BytesIO(data), filename=fname),
                                   caption="📅 File ICS untuk 30 hari ke depan.\nNama event: <b>Nama Customer + Singkatan Paket</b>.\nUID stabil → meminimalkan duplikasi saat impor ulang.",
                                   parse_mode=ParseMode.HTML)
    rows = list_numbers(q.from_user.id)
    sort_order, search_query = get_prefs(q.from_user.id)
    await q.message.reply_text("Selesai. Kembali ke menu:",
                               reply_markup=main_menu_keyboard(bool(rows), sort_order, bool(search_query), q.from_user.id))

# =========================
# HELP
# =========================
async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sort_order, search_query = get_prefs(q.from_user.id)
    text = (
        "ℹ️ <b>Bantuan</b>\n"
        "• Overview: urut sisa hari & pencarian.\n"
        "• Masa aktif kartu ditampilkan (tanpa notifikasi).\n"
        "• Detail Cepat: paket + 3 kuota teratas, tombol ♻️ paksa & 📋 salin nomor.\n"
        "• Refresh: 🔄 due-only (aman) atau ♻️ paksa semua (abaikan interval & blokir lokal, jeda 1.5s).\n"
        "• Export ICS: event all-day, judul = Nama + Singkatan Paket, UID stabil.\n"
        "• Reminder H-1 & Hari-H: paket yang habis besok/hari ini (atur di Pengaturan).\n"
        "• Backup mingguan + restore (admin only). Tombol ada di menu utama.\n"
        "\n"
        "📌 <b>Indikator Paket</b>: 🟢 aman (>7 hari), 🟡 waspada (≤7 hari), 🔴 segera (≤3 hari), ⚪ unknown/error.\n"
        f"📌 <b>Kebijakan</b>: refresh otomatis tiap <b>{int(REFRESH_INTERVAL_SECS/3600)} jam</b>.\n"
        "\n"
        "💡 <b>Menyalin Nomor</b>: nomor ditulis monospace <code>seperti ini</code> → tap & tahan untuk Copy. "
        "Atau gunakan tombol <b>📋 Salin nomor</b> untuk mengisi kolom pesan."
    )
    await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                              reply_markup=main_menu_keyboard(True, sort_order, bool(search_query), q.from_user.id))

async def render_settings_menu(query) -> None:
    h1, h0, hour = get_reminder_settings(query.from_user.id)
    status = lambda flag: "✅ Aktif" if flag else "❌ Nonaktif"
    text = (
        "⚙️ <b>Pengaturan Reminder</b>\n"
        f"• Reminder H-1: {status(h1)}\n"
        f"• Reminder Hari-H: {status(h0)}\n"
        f"• Jam pengiriman (WIB): <b>{hour:02d}:00</b>\n"
        "\n"
        "Reminder dikirim sesuai cache terakhir. Pastikan auto-refresh berjalan atau lakukan refresh manual."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(("✅ " if h1 else "❌ ") + "Toggle H-1", callback_data="settings_toggle:reminder_h1"),
            InlineKeyboardButton(("✅ " if h0 else "❌ ") + "Toggle Hari-H", callback_data="settings_toggle:reminder_h0"),
        ],
        [InlineKeyboardButton("🕘 Ubah jam kirim", callback_data="settings_hour")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="back_to_menu")],
    ])
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def render_hour_picker(query) -> None:
    buttons = []
    row = []
    for hour in range(24):
        row.append(InlineKeyboardButton(f"{hour:02d}:00", callback_data=f"settings_hour_pick:{hour}"))
        if len(row) == 6:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Batal", callback_data="menu_settings")])
    await query.edit_message_text(
        "🕘 <b>Pilih jam pengiriman (WIB)</b>\nReminder akan dikirim di jam terpilih jika ada paket yang memenuhi kriteria.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await render_settings_menu(q)

async def settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, column = q.data.split(":", 1)
    await q.answer()
    toggle_reminder_flag(q.from_user.id, column)
    await render_settings_menu(q)

async def settings_hour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await render_hour_picker(q)

async def settings_hour_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, hour_str = q.data.split(":", 1)
    hour = int(hour_str)
    set_reminder_hour(q.from_user.id, hour)
    await q.answer(f"Jam reminder diset ke {hour:02d}:00 WIB")
    await render_settings_menu(q)

# No-op & cancel
async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Dibatalkan.")
    return ConversationHandler.END

# Global error handler
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Exception while handling an update: %s", context.error)

# =========================
# REMINDER JOB (H-1 paket)
# =========================
async def reminder_job(app: Application):
    now_wib = datetime.now(TZ)
    today_wib = now_wib.date()
    tomorrow_wib = today_wib + timedelta(days=1)
    current_hour = now_wib.hour

    con = _db_conn(); cur = con.cursor()
    cur.execute("""
        SELECT n.tg_user_id, n.label, n.msisdn, n.last_payload,
               n.last_notified_expiry, n.last_notified_type, n.last_notified_at
        FROM numbers n
        ORDER BY n.tg_user_id, n.created_at ASC
    """)
    rows = cur.fetchall(); con.close()

    for r in rows:
        tg_user_id = r["tg_user_id"]
        label = r["label"] or "Customer"
        msisdn = r["msisdn"]
        last_payload_json = r["last_payload"]
        last_notified_expiry = r["last_notified_expiry"] or ""
        last_notified_type = (r["last_notified_type"] or "").upper()

        h1_enabled, h0_enabled, pref_hour = get_reminder_settings(tg_user_id)
        if current_hour != pref_hour:
            continue

        if not last_payload_json:
            continue

        try:
            payload = json.loads(last_payload_json)
        except Exception:
            continue

        data = payload.get("data") or {}
        pk_info = data.get("package_info") or {}
        packages = pk_info.get("packages") or []
        if not packages:
            continue

        due_h1 = []
        due_h0 = []
        for pkg in packages:
            expiry_text = pkg.get("expiry")
            if expiry_text in (None, "", "-"):
                continue
            d = parse_expiry_text(expiry_text)
            if not d:
                continue
            if d == tomorrow_wib:
                due_h1.append(pkg)
            elif d == today_wib:
                due_h0.append(pkg)

        async def send_reminder(due_pkgs: List[Dict[str, Any]], notif_type: str, day_desc: str, suffix: str):
            if not due_pkgs:
                return
            expiry_text = due_pkgs[0].get("expiry", "-")
            if notif_type == last_notified_type and last_notified_expiry == expiry_text:
                return
            emoji, _, _ = indicator_by_date(expiry_text)
            pkg_lines = reminder_package_lines(due_pkgs)
            lines = [
                f"🔔 <b>Pengingat Paket {day_desc}</b>",
                f"👤 <b>{html.escape(label)}</b>",
                f"📱 <code>{html.escape(msisdn)}</code>",
                "",
                f"{emoji} Paket yang {suffix}:",
                *pkg_lines,
                "",
                "Pastikan pelanggan mendapatkan kuota baru atau lakukan pengecekan ulang setelah isi ulang."
            ]
            text = "\n".join(lines)
            try:
                await app.bot.send_message(chat_id=tg_user_id, text=text, parse_mode=ParseMode.HTML)
                set_last_notified(msisdn, expiry_text, notif_type)
                logging.info(f"[Reminder] Sent {notif_type} for {msisdn} ({label}) exp {expiry_text}")
            except Exception as e:
                logging.error(f"[Reminder] Failed to send to {tg_user_id}: {e}")

        if h1_enabled:
            await send_reminder(due_h1, "H-1", "H-1", "akan habis besok")
        if h0_enabled:
            await send_reminder(due_h0, "H", "Hari-H", "habis hari ini")

# =========================
# SCHEDULER TASKS
# =========================
async def scheduled_refresh(app: Application):
    now = int(time.time())
    con = _db_conn(); cur = con.cursor()
    cur.execute("SELECT msisdn, COALESCE(last_fetch_ts,0), COALESCE(next_retry_ts,0) FROM numbers")
    rows = cur.fetchall(); con.close()
    for msisdn, last_ts, next_retry in rows:
        last_ts = last_ts or 0
        next_retry = next_retry or 0
        if (now - last_ts) >= REFRESH_INTERVAL_SECS and now >= next_retry:
            ok, m = fetch_and_cache(msisdn)
            logging.info(f"[Scheduler] Refresh {msisdn}: {'OK' if ok else 'ERR'} - {m}")

# =========================
# BACKUP & RESTORE (ADMIN)
# =========================
def _gzip_file(src_path: str, dst_path: str):
    with open(src_path, "rb") as f_in, gzip.open(dst_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

def _rotate_backups(directory: str, prefix: str, keep: int = 7):
    p = Path(directory)
    files = sorted(p.glob(f"{prefix}-*.db.gz"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except Exception:
            pass

async def send_backup_to_admins(app: Application, caption: str):
    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    gz_path = os.path.join(BACKUP_DIR, f"backup-{ts}.db.gz")
    try:
        _gzip_file(DB_PATH, gz_path)
    except Exception as e:
        logging.error(f"Backup failed: {e}")
        return
    _rotate_backups(BACKUP_DIR, "backup", keep=7)

    if ADMIN_IDS:
        targets = list(ADMIN_IDS)
    else:
        con = _db_conn(); cur = con.cursor()
        cur.execute("SELECT DISTINCT tg_user_id FROM users")
        targets = [r[0] for r in cur.fetchall()]
        con.close()

    for uid in targets:
        try:
            with open(gz_path, "rb") as f:
                await app.bot.send_document(chat_id=uid, document=InputFile(f, filename=os.path.basename(gz_path)),
                                            caption=caption)
        except Exception as e:
            logging.error(f"Send backup to {uid} failed: {e}")

async def weekly_backup_job(app: Application):
    await send_backup_to_admins(app, caption="🗄 Backup mingguan database XL Reminder (otomatis).")

async def backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Hanya admin.", parse_mode=ParseMode.HTML); return
    await send_backup_to_admins(context.application, caption="🗄 Backup manual database XL Reminder.")
    await update.message.reply_text("✅ Backup dikirim.", parse_mode=ParseMode.HTML)

async def menu_backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Hanya admin.", show_alert=True); return
    await q.edit_message_text("⏳ Membuat & mengirim backup…")
    await send_backup_to_admins(context.application, caption="🗄 Backup manual database XL Reminder.")
    rows = list_numbers(q.from_user.id)
    sort_order, search_query = get_prefs(q.from_user.id)
    await q.edit_message_text("✅ Backup terkirim.\n\n🏠 Kembali ke menu:",
                              reply_markup=main_menu_keyboard(bool(rows), sort_order, bool(search_query), q.from_user.id))

async def restore_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        if update.callback_query:
            await update.callback_query.answer("Hanya admin.", show_alert=True)
        else:
            await update.message.reply_text("❌ Hanya admin.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.edit_message_text("⬆️ Kirim file <b>.db</b> atau <b>.db.gz</b> untuk di-restore.\n"
                                                      "⚠️ Data saat ini akan digantikan.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("⬆️ Kirim file <b>.db</b> atau <b>.db.gz</b> untuk di-restore.\n"
                                        "⚠️ Data saat ini akan digantikan.", parse_mode=ParseMode.HTML)
    return RESTORE_WAIT_FILE

async def restore_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        await update.message.reply_text("❌ Harap kirim file .db / .db.gz.", parse_mode=ParseMode.HTML)
        return RESTORE_WAIT_FILE
    doc = update.message.document
    fn = doc.file_name or ""
    if not (fn.endswith(".db") or fn.endswith(".db.gz")):
        await update.message.reply_text("❌ Ekstensi tidak dikenal. Kirim .db atau .db.gz.", parse_mode=ParseMode.HTML)
        return RESTORE_WAIT_FILE

    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    dl_path = os.path.join(BACKUP_DIR, f"upload-{ts}-{fn}")
    file = await doc.get_file()
    await file.download_to_drive(dl_path)

    if dl_path.endswith(".gz"):
        out_path = dl_path[:-3]
        with gzip.open(dl_path, "rb") as f_in, open(out_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        new_db = out_path
    else:
        new_db = dl_path

    try:
        test_con = sqlite3.connect(new_db); test_con.close()
        old_backup = os.path.join(BACKUP_DIR, f"pre-restore-{ts}.db.gz")
        _gzip_file(DB_PATH, old_backup)
        os.replace(new_db, DB_PATH)
        db_init()
        await update.message.reply_text("✅ Restore sukses. Schema dimigrasi bila perlu.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Restore gagal: {e}", parse_mode=ParseMode.HTML)
    return ConversationHandler.END

# =========================
# APP
# =========================
def build_app() -> Application:
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    # Add number
    conv_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_add, pattern="^menu_add$")],
        states={
            ASK_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_number)],
            ASK_LABEL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_label)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True, name="add-number"
    )

    # Edit label
    conv_edit = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_edit, pattern="^menu_edit$")],
        states={
            EDIT_PICK:  [CallbackQueryHandler(edit_pick, pattern="^edit:")],
            EDIT_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_label)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True, name="edit-label"
    )

    # Search
    conv_search = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_search, pattern="^menu_search$")],
        states={ASK_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_search)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True, name="search"
    )

    # Restore (admin) — entry via command & tombol callback
    conv_restore = ConversationHandler(
        entry_points=[
            CommandHandler("restore", restore_begin),
            CallbackQueryHandler(restore_begin, pattern="^menu_restore$")
        ],
        states={RESTORE_WAIT_FILE: [MessageHandler(filters.Document.ALL, restore_receive_file)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True, name="restore"
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(menu_overview, pattern="^menu_overview$"))

    app.add_handler(conv_add)
    app.add_handler(conv_edit)
    app.add_handler(conv_search)
    app.add_handler(conv_restore)

    app.add_handler(CallbackQueryHandler(menu_search_clear, pattern="^menu_search_clear$"))
    app.add_handler(CallbackQueryHandler(menu_sort_toggle, pattern="^menu_sort_toggle$"))

    app.add_handler(CallbackQueryHandler(menu_delete,        pattern="^menu_delete$"))
    app.add_handler(CallbackQueryHandler(delete_selected,    pattern="^del:"))
    app.add_handler(CallbackQueryHandler(menu_check,         pattern="^menu_check$"))
    app.add_handler(CallbackQueryHandler(check_selected,     pattern="^chk:"))
    app.add_handler(CallbackQueryHandler(check_refresh_due,        pattern="^chk_refresh_due$"))
    app.add_handler(CallbackQueryHandler(check_refresh_force_all,  pattern="^chk_refresh_force_all$"))

    app.add_handler(CallbackQueryHandler(menu_quick,         pattern="^menu_quick$"))
    app.add_handler(CallbackQueryHandler(quick_detail_selected, pattern="^qdet:"))
    app.add_handler(CallbackQueryHandler(quick_force_one,       pattern="^qforce:"))

    app.add_handler(CallbackQueryHandler(menu_ics,           pattern="^menu_ics$"))
    app.add_handler(CallbackQueryHandler(menu_settings,      pattern="^menu_settings$"))
    app.add_handler(CallbackQueryHandler(settings_toggle,    pattern="^settings_toggle:"))
    app.add_handler(CallbackQueryHandler(settings_hour,      pattern="^settings_hour$"))
    app.add_handler(CallbackQueryHandler(settings_hour_pick, pattern="^settings_hour_pick:"))

    # Backup: command & tombol
    app.add_handler(CommandHandler("backup_now", backup_now))
    app.add_handler(CallbackQueryHandler(menu_backup_now,    pattern="^menu_backup_now$"))

    app.add_handler(CallbackQueryHandler(menu_help,          pattern="^menu_help$"))
    app.add_handler(CallbackQueryHandler(noop,               pattern="^noop:|^noop$"))

    app.add_error_handler(on_error)
    return app

async def on_startup(app: Application):
    logging.info("XL Reminder Bot started.")
    sched = AsyncIOScheduler(timezone=TZ)
    # scan refresh 30 menit
    sched.add_job(lambda: scheduled_refresh(app), "interval", minutes=30, id="refresh_scan")
    # reminder scan per jam (hormati jam preferensi tiap user)
    sched.add_job(lambda: reminder_job(app),
                  CronTrigger(minute=0, timezone=TZ),
                  id="reminder_hourly")
    # backup mingguan
    sched.add_job(lambda: weekly_backup_job(app),
                  CronTrigger(day_of_week=WEEKLY_BACKUP_DAY, hour=WEEKLY_BACKUP_HOUR, minute=0, timezone=TZ),
                  id="weekly_backup")
    sched.start()
    logging.info(f"Scheduler aktif: refresh 6 jam (scan 30m), reminder per jam (default {REMINDER_HOUR:02d}:00 WIB), backup mingguan {WEEKLY_BACKUP_DAY} {WEEKLY_BACKUP_HOUR:02d}:00.")

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN kosong. Set di .env")
    app = build_app()
    app.post_init = on_startup
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
