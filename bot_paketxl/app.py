"""Core XL Reminder bot application."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional
import asyncio
import gzip
import html
import io
import json
import logging
import shutil
import subprocess
import sys
import time

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Application, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .api import XLApiClient
from .config import AppConfig
from .formatting import indicator_by_date, primary_package_info
from .storage import NumberRecord, Storage
from .telegram_utils import bot_send_in_chunks, edit_or_reply_in_chunks, reply_in_chunks
from .views import (
    build_detail_message,
    build_overview_entry,
    build_overview_message,
    build_reminder_message,
    normalize_number,
)


@dataclass
class KeyboardFactory:
    config: AppConfig
    storage: Storage

    def is_admin(self, user_id: int) -> bool:
        if self.config.admin_ids:
            return user_id in self.config.admin_ids
        return True

    def main_menu(self, tg_user_id: int) -> InlineKeyboardMarkup:
        prefs = self.storage.get_prefs(tg_user_id)
        has_numbers = bool(self.storage.list_numbers(tg_user_id))
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("📊 Overview", callback_data="menu_overview")],
            [InlineKeyboardButton("➕ Daftarkan Nomor", callback_data="menu_add")],
        ]
        if has_numbers:
            sort_txt = "↕️ Urut (Sisa Hari) ↑" if prefs.sort_order == "asc" else "↕️ Urut (Sisa Hari) ↓"
            search_row = [InlineKeyboardButton("🔎 Cari", callback_data="menu_search")]
            if prefs.search_query:
                search_row.append(InlineKeyboardButton("🧹 Hapus Filter", callback_data="menu_search_clear"))
            rows.extend(
                [
                    [InlineKeyboardButton(sort_txt, callback_data="menu_sort_toggle")],
                    search_row,
                    [
                        InlineKeyboardButton("✏️ Edit Nama", callback_data="menu_edit"),
                        InlineKeyboardButton("🗑 Hapus Nomor", callback_data="menu_delete"),
                    ],
                    [
                        InlineKeyboardButton("✅ Cek Sekarang", callback_data="menu_check"),
                        InlineKeyboardButton("🔍 Detail Cepat", callback_data="menu_quick"),
                    ],
                    [InlineKeyboardButton("📅 Export ICS (30 hari)", callback_data="menu_ics")],
                ]
            )
        if self.is_admin(tg_user_id):
            rows.append(
                [
                    InlineKeyboardButton("🗄 Backup", callback_data="menu_backup_now"),
                    InlineKeyboardButton("♻️ Restore", callback_data="menu_restore"),
                ]
            )
            rows.append([InlineKeyboardButton("🔁 Update Bot", callback_data="menu_update")])
        rows.append([InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu_settings")])
        rows.append([InlineKeyboardButton("ℹ️ Bantuan", callback_data="menu_help")])
        return InlineKeyboardMarkup(rows)

    def numbers(self, records: List[NumberRecord], prefix: str, with_refresh: bool = False) -> InlineKeyboardMarkup:
        buttons: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for record in records:
            label = record.label or record.msisdn
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{record.msisdn}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        if with_refresh:
            buttons.append(
                [
                    InlineKeyboardButton("🔄 Refresh due", callback_data=f"{prefix}_refresh_due"),
                    InlineKeyboardButton("♻️ Paksa semua", callback_data=f"{prefix}_refresh_force_all"),
                ]
            )
        buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="back_to_menu")])
        return InlineKeyboardMarkup(buttons)

    def single_back(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="back_to_menu")]])


class XLReminderApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.db_path, config.default_reminder_hour)
        self.storage.migrate()
        self.api = XLApiClient(config.api_template, config.request_timeout)
        self.keyboard = KeyboardFactory(config, self.storage)
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        self.tz = pytz.timezone(config.timezone)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def ensure_token(self) -> None:
        self.config.ensure_token()

    def _main_menu(self, user_id: int) -> InlineKeyboardMarkup:
        return self.keyboard.main_menu(user_id)

    def _overview_text(self, user_id: int) -> str:
        records = self.storage.list_numbers(user_id)
        prefs = self.storage.get_prefs(user_id)
        filtered: list[NumberRecord] = []
        query = prefs.search_query.lower()
        for record in records:
            if query and query not in (record.label or "").lower() and query not in record.msisdn:
                continue
            filtered.append(record)

        def key(record: NumberRecord) -> int:
            _, payload, _, _, _, _, _ = self.storage.get_cached(record.msisdn)
            if not payload:
                return 9999 if prefs.sort_order == "asc" else -9999
            data = payload.get("data") or {}
            pk_info = data.get("package_info") or {}
            packages = pk_info.get("packages") or []
            if not packages:
                return 9999
            expiry_text = packages[0].get("expiry") or ""
            indicator = indicator_by_date(expiry_text)
            return indicator.days_left if indicator.days_left is not None else 9999

        filtered.sort(key=key, reverse=prefs.sort_order == "desc")
        sections: list[str] = []
        for record in filtered:
            last_fetch_ts, payload, last_error, next_retry, _, _, _ = self.storage.get_cached(record.msisdn)
            pkg_label = "Belum cek"
            expiry_text = "-"
            quotas_line = ""
            if payload:
                data = payload.get("data") or {}
                pk_info = data.get("package_info") or {}
                packages = pk_info.get("packages") or []
                if packages:
                    first = packages[0]
                    abbr, expiry_text, full_name = primary_package_info(data)
                    pkg_label = full_name or abbr
                    quotas = first.get("quotas") or []
                    if quotas:
                        q = quotas[0]
                        remaining = q.get("remaining", "-")
                        total = q.get("total", "-")
                        quotas_line = f"📊 Sisa utama: <b>{html.escape(str(remaining))}</b> / {html.escape(str(total))}"
            indicator = indicator_by_date(expiry_text)
            sections.append(
                build_overview_entry(
                    record.label or record.msisdn,
                    record.msisdn,
                    indicator,
                    pkg_label,
                    expiry_text,
                    quotas_line,
                    last_fetch_ts,
                    error=last_error,
                    blocked_until=next_retry,
                )
            )
        if not sections:
            return "Belum ada nomor terdaftar. Tekan <b>➕ Daftarkan Nomor</b> untuk menambahkan."
        return build_overview_message(sections)

    def _ics_escape(self, s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    def _build_ics(self, user_id: int, days_ahead: int = 30) -> bytes:
        records = self.storage.list_numbers(user_id)
        today = datetime.now(self.tz).date()
        limit = today + timedelta(days=days_ahead)
        events: list[str] = []
        for record in records:
            _, payload, _, _, _, _, _ = self.storage.get_cached(record.msisdn)
            if not payload:
                continue
            data = payload.get("data") or {}
            abbr, expiry, full_name = primary_package_info(data)
            if abbr.startswith("ERROR:") or not expiry or expiry == "-":
                continue
            parsed = indicator_by_date(expiry)
            if parsed.days_left is None:
                continue
            try:
                event_date = datetime.strptime(expiry, "%d-%m-%Y").date()
            except ValueError:
                continue
            if event_date > limit:
                continue
            label = record.label or "Customer"
            title = f"{label} {abbr}".strip()
            uid = f"{record.msisdn}-{event_date.strftime('%Y%m%d')}-{abbr.replace(' ','')}-xlreminder"
            dtstamp = datetime.now(self.tz).strftime("%Y%m%dT%H%M%S")
            dtstart = event_date.strftime("%Y%m%d")
            dtend = (event_date + timedelta(days=1)).strftime("%Y%m%d")
            desc = (
                f"Nomor: {record.msisdn}\\n"
                f"Paket: {self._ics_escape(full_name or abbr)}\\n"
                "Dibuat oleh XL Reminder Bot"
            )
            events.append(
                "\r\n".join(
                    [
                        "BEGIN:VEVENT",
                        f"UID:{uid}",
                        f"DTSTAMP:{dtstamp}",
                        f"SUMMARY:{self._ics_escape(title)}",
                        "TRANSP:OPAQUE",
                        "CLASS:PUBLIC",
                        "STATUS:CONFIRMED",
                        f"DTSTART;VALUE=DATE:{dtstart}",
                        f"DTEND;VALUE=DATE:{dtend}",
                        f"DESCRIPTION:{desc}",
                        "END:VEVENT",
                    ]
                )
            )
        calendar = "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "PRODID:-//XL-Reminder//ID",
                "VERSION:2.0",
                "CALSCALE:GREGORIAN",
                "METHOD:PUBLISH",
                *events,
                "END:VCALENDAR",
                "",
            ]
        )
        return calendar.encode("utf-8")

    def _current_commit(self) -> str:
        if shutil.which(self.config.git_bin) is None:
            return "git tidak tersedia"
        try:
            result = subprocess.run(
                [self.config.git_bin, "rev-parse", "--short", "HEAD"],
                cwd=self.config.app_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() or "(unknown)"
        except Exception:
            return "(gagal membaca)"

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def build_application(self) -> Application:
        app = Application.builder().token(self.config.bot_token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help_command))
        app.add_handler(CallbackQueryHandler(self.back_to_menu, pattern="^back_to_menu$"))
        app.add_handler(CallbackQueryHandler(self.menu_overview, pattern="^menu_overview$"))
        app.add_handler(CallbackQueryHandler(self.menu_sort_toggle, pattern="^menu_sort_toggle$"))
        app.add_handler(CallbackQueryHandler(self.menu_search, pattern="^menu_search$"))
        app.add_handler(CallbackQueryHandler(self.menu_search_clear, pattern="^menu_search_clear$"))
        app.add_handler(CallbackQueryHandler(self.menu_add, pattern="^menu_add$"))
        app.add_handler(CallbackQueryHandler(self.menu_edit, pattern="^menu_edit$"))
        app.add_handler(CallbackQueryHandler(self.edit_pick, pattern="^edit:"))
        app.add_handler(CallbackQueryHandler(self.menu_delete, pattern="^menu_delete$"))
        app.add_handler(CallbackQueryHandler(self.delete_confirm, pattern="^delete:"))
        app.add_handler(CallbackQueryHandler(self.menu_check, pattern="^menu_check$"))
        app.add_handler(CallbackQueryHandler(self.check_number, pattern="^check:"))
        app.add_handler(CallbackQueryHandler(self.check_refresh_due, pattern="^check_refresh_due$"))
        app.add_handler(CallbackQueryHandler(self.check_force_all, pattern="^check_refresh_force_all$"))
        app.add_handler(CallbackQueryHandler(self.menu_quick, pattern="^menu_quick$"))
        app.add_handler(CallbackQueryHandler(self.quick_show, pattern="^quick:"))
        app.add_handler(CallbackQueryHandler(self.menu_ics, pattern="^menu_ics$"))
        app.add_handler(CallbackQueryHandler(self.ics_export, pattern="^ics:"))
        app.add_handler(CallbackQueryHandler(self.menu_update, pattern="^menu_update$"))
        app.add_handler(CallbackQueryHandler(self.menu_update_run, pattern="^menu_update_run$"))
        app.add_handler(CallbackQueryHandler(self.menu_backup_now, pattern="^menu_backup_now$"))
        app.add_handler(CallbackQueryHandler(self.menu_restore, pattern="^menu_restore$"))
        app.add_handler(CallbackQueryHandler(self.menu_settings, pattern="^menu_settings$"))
        app.add_handler(CallbackQueryHandler(self.settings_toggle, pattern="^settings_toggle:"))
        app.add_handler(CallbackQueryHandler(self.settings_hour, pattern="^settings_hour$"))
        app.add_handler(CallbackQueryHandler(self.menu_help, pattern="^menu_help$"))
        app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))
        app.add_error_handler(self.on_error)
        return app

    # ------------------------------------------------------------------
    # basic commands
    # ------------------------------------------------------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        self.storage.ensure_user(user)
        overview = self._overview_text(user.id)
        intro = "👋 <b>Selamat datang di XL Reminder Bot</b>\n\n"
        outro = "\n— — —\nGunakan tombol di bawah ini 👇"
        text = f"{intro}{overview}{outro}"
        markup = self._main_menu(user.id)
        if update.message:
            await reply_in_chunks(update.message, text, reply_markup=markup, limit=self.config.message_chunk)
        elif update.callback_query:
            await edit_or_reply_in_chunks(update.callback_query, text, reply_markup=markup, limit=self.config.message_chunk)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_help(update, context)

    async def menu_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        await self._send_help(update, context)

    async def _send_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "ℹ️ <b>Bantuan</b>\n\n"
            "• Tambahkan nomor pelanggan melalui menu ➕.\n"
            "• Overview menampilkan ringkasan paket aktif lengkap dengan kuota utama.\n"
            "• Pengingat H-1/Hari-H dapat ditata lewat menu Pengaturan.\n"
            "• Admin dapat melakukan backup, restore, serta update bot langsung dari Telegram."
        )
        markup = self.keyboard.single_back()
        if update.callback_query:
            await edit_or_reply_in_chunks(update.callback_query, text, reply_markup=markup, limit=self.config.message_chunk)
        elif update.message:
            await reply_in_chunks(update.message, text, reply_markup=markup, limit=self.config.message_chunk)

    # ------------------------------------------------------------------
    # menu handlers
    # ------------------------------------------------------------------
    async def back_to_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        context.user_data.pop("flow", None)
        await query.edit_message_text(
            "🏠 <b>Menu Utama</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=self._main_menu(query.from_user.id),
        )

    async def menu_overview(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("⏳ Membuat overview…")
        overview = self._overview_text(query.from_user.id)
        await edit_or_reply_in_chunks(query, overview, reply_markup=self._main_menu(query.from_user.id), limit=self.config.message_chunk)

    async def menu_sort_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        prefs = self.storage.get_prefs(query.from_user.id)
        new_order = "desc" if prefs.sort_order == "asc" else "asc"
        self.storage.update_sort_order(query.from_user.id, new_order)
        await self.menu_overview(update, context)

    async def menu_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        context.user_data["flow"] = {"name": "search"}
        await query.edit_message_text(
            "🔎 Kirim kata kunci untuk <b>mencari</b> (nama atau nomor).",
            parse_mode=ParseMode.HTML,
        )

    async def menu_search_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        self.storage.clear_search(query.from_user.id)
        await self.menu_overview(update, context)

    async def menu_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        context.user_data["flow"] = {"name": "add", "step": "number"}
        await query.edit_message_text(
            "➕ Kirim nomor XL kamu (0819… / +62819… / 62819…).",
            parse_mode=ParseMode.HTML,
        )

    async def menu_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        records = self.storage.list_numbers(query.from_user.id)
        if not records:
            await query.edit_message_text(
                "Belum ada nomor untuk diubah.",
                reply_markup=self._main_menu(query.from_user.id),
            )
            return
        await query.edit_message_text(
            "Pilih nomor yang ingin diubah namanya:",
            reply_markup=self.keyboard.numbers(records, "edit"),
        )

    async def edit_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, msisdn = query.data.split(":", 1)
        context.user_data["flow"] = {"name": "edit", "msisdn": msisdn}
        await query.edit_message_text(
            f"Masukkan label baru untuk <code>{html.escape(msisdn)}</code>:",
            parse_mode=ParseMode.HTML,
        )

    async def menu_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        records = self.storage.list_numbers(query.from_user.id)
        if not records:
            await query.edit_message_text(
                "Belum ada nomor.",
                reply_markup=self._main_menu(query.from_user.id),
            )
            return
        await query.edit_message_text(
            "Pilih nomor yang ingin dihapus:",
            reply_markup=self.keyboard.numbers(records, "delete"),
        )

    async def delete_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, msisdn = query.data.split(":", 1)
        count = self.storage.delete_number(query.from_user.id, msisdn)
        msg = "✅ Nomor dihapus." if count else "⚠️ Nomor tidak ditemukan."
        await query.edit_message_text(msg, reply_markup=self._main_menu(query.from_user.id))

    async def menu_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        records = self.storage.list_numbers(query.from_user.id)
        if not records:
            await query.edit_message_text(
                "Belum ada nomor terdaftar.",
                reply_markup=self._main_menu(query.from_user.id),
            )
            return
        await query.edit_message_text(
            "Pilih nomor untuk me-refresh data:",
            reply_markup=self.keyboard.numbers(records, "check", with_refresh=True),
        )

    async def check_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, msisdn = query.data.split(":", 1)
        result = self.api.fetch(msisdn)
        if result.success:
            self.storage.update_cache(msisdn, result.payload, None)
            text = "✅ Data diperbarui."
        else:
            self.storage.update_cache(msisdn, result.payload, result.message, result.block_seconds)
            text = f"⚠️ {html.escape(result.message)}"
        await query.edit_message_text(text, reply_markup=self._main_menu(query.from_user.id))

    async def check_refresh_due(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        now = int(time.time())
        count = 0
        for record in self.storage.list_numbers(query.from_user.id):
            last_ts, _, _, next_retry, _, _, _ = self.storage.get_cached(record.msisdn)
            if (now - last_ts) >= self.config.refresh_interval_seconds and now >= next_retry:
                result = self.api.fetch(record.msisdn)
                if result.success:
                    self.storage.update_cache(record.msisdn, result.payload, None)
                else:
                    self.storage.update_cache(record.msisdn, result.payload, result.message, result.block_seconds)
                count += 1
        await query.edit_message_text(
            f"✅ Refresh otomatis selesai untuk {count} nomor.",
            reply_markup=self._main_menu(query.from_user.id),
        )

    async def check_force_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        count = 0
        for record in self.storage.list_numbers(query.from_user.id):
            result = self.api.fetch(record.msisdn)
            if result.success:
                self.storage.update_cache(record.msisdn, result.payload, None)
            else:
                self.storage.update_cache(record.msisdn, result.payload, result.message, result.block_seconds)
            count += 1
        await query.edit_message_text(
            f"♻️ Refresh paksa selesai untuk {count} nomor.",
            reply_markup=self._main_menu(query.from_user.id),
        )

    async def menu_quick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        records = self.storage.list_numbers(query.from_user.id)
        if not records:
            await query.edit_message_text(
                "Belum ada nomor.",
                reply_markup=self._main_menu(query.from_user.id),
            )
            return
        await query.edit_message_text(
            "Pilih nomor untuk melihat detail cache:",
            reply_markup=self.keyboard.numbers(records, "quick"),
        )

    async def quick_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, msisdn = query.data.split(":", 1)
        _, payload, last_error, _, _, _, _ = self.storage.get_cached(msisdn)
        if payload:
            text = build_detail_message(payload.get("data") or {})
        elif last_error:
            text = f"🚫 <i>{html.escape(last_error)}</i>"
        else:
            text = "⚠️ Belum ada data untuk nomor ini."
        await edit_or_reply_in_chunks(query, text, reply_markup=self.keyboard.single_back(), limit=self.config.message_chunk)

    async def menu_ics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        records = self.storage.list_numbers(query.from_user.id)
        if not records:
            await query.edit_message_text(
                "Belum ada nomor.",
                reply_markup=self._main_menu(query.from_user.id),
            )
            return
        await query.edit_message_text(
            "Pilih nomor untuk export ICS:",
            reply_markup=self.keyboard.numbers(records, "ics"),
        )

    async def ics_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, msisdn = query.data.split(":", 1)
        _, payload, _, _, _, _, _ = self.storage.get_cached(msisdn)
        if not payload:
            await query.edit_message_text(
                "⚠️ Tidak ada cache untuk dibuatkan ICS.",
                reply_markup=self.keyboard.single_back(),
            )
            return
        data = payload.get("data") or {}
        pk_info = data.get("package_info") or {}
        packages = pk_info.get("packages") or []
        if not packages:
            await query.edit_message_text(
                "⚠️ Paket tidak ditemukan.",
                reply_markup=self.keyboard.single_back(),
            )
            return
        ics = self._build_ics(query.from_user.id)
        fname = f"xl-expiry-{datetime.now(self.tz).strftime('%Y%m%d')}.ics"
        await query.message.reply_document(
            document=InputFile(io.BytesIO(ics), filename=fname),
            caption="📅 File ICS untuk 30 hari ke depan. UID stabil sehingga aman untuk impor ulang.",
            parse_mode=ParseMode.HTML,
        )

    async def menu_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.keyboard.is_admin(query.from_user.id):
            await query.answer("Hanya admin.", show_alert=True)
            return
        await query.edit_message_text(
            (
                "🔁 <b>Update Bot</b>\n\n"
                f"Repo: <code>{html.escape(self.config.repo_url)}</code>\n"
                f"Branch: <code>{html.escape(self.config.repo_branch)}</code>\n"
                f"Commit saat ini: <code>{html.escape(self._current_commit())}</code>\n\n"
                "Tekan tombol untuk menarik pembaruan terbaru."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Tarik Pembaruan", callback_data="menu_update_run")],
                    [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_overview")],
                ]
            ),
        )

    async def menu_update_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.keyboard.is_admin(query.from_user.id):
            await query.answer("Hanya admin.", show_alert=True)
            return
        if shutil.which(self.config.git_bin) is None:
            await query.answer("git tidak tersedia", show_alert=True)
            return
        await query.edit_message_text("🔁 Menjalankan update dari GitHub…", parse_mode=ParseMode.HTML)
        commands = [
            ("Fetch", [self.config.git_bin, "fetch", "--all", "--prune"], self.config.app_dir),
            (
                "Reset",
                [self.config.git_bin, "reset", "--hard", f"origin/{self.config.repo_branch}"],
                self.config.app_dir,
            ),
        ]
        requirements = self.config.app_dir / "requirements.txt"
        python_exec = self.config.venv_path / "bin" / "python"
        if not python_exec.exists():
            python_exec = Path(sys.executable)
        commands.append(
            (
                "Install deps",
                [str(python_exec), "-m", "pip", "install", "-r", str(requirements)],
                self.config.app_dir,
            )
        )
        logs: list[str] = []
        success = True
        for label, cmd, cwd in commands:
            logs.append(f"$ {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if out:
                logs.append(out.decode())
            if err:
                logs.append(err.decode())
            if proc.returncode != 0:
                logs.append(f"❌ {label} gagal dengan kode {proc.returncode}")
                success = False
                break
            logs.append(f"✅ {label} selesai")
        summary = "✅ Update selesai." if success else "⚠️ Update gagal."
        body = "\n".join([line for line in logs if line]).strip()
        if len(body) > 3500:
            body = body[-3500:]
        await query.edit_message_text(
            f"{summary}\n\n<pre>{html.escape(body or '(tidak ada log)')}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="menu_overview")]]),
        )

    async def menu_backup_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.keyboard.is_admin(query.from_user.id):
            await query.answer("Hanya admin.", show_alert=True)
            return
        await query.edit_message_text("⏳ Membuat & mengirim backup…")
        await self._send_backup(context.application, "🗄 Backup manual database XL Reminder.")
        await query.edit_message_text(
            "✅ Backup terkirim.\n\n🏠 Kembali ke menu:",
            reply_markup=self._main_menu(query.from_user.id),
        )

    async def menu_restore(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.keyboard.is_admin(query.from_user.id):
            await query.answer("Hanya admin.", show_alert=True)
            return
        context.user_data["flow"] = {"name": "restore"}
        await query.edit_message_text(
            "⬆️ Kirim file <b>.db</b> atau <b>.db.gz</b> untuk di-restore.⚠️ Data saat ini akan digantikan.",
            parse_mode=ParseMode.HTML,
        )

    async def menu_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        prefs = self.storage.get_prefs(query.from_user.id)
        text = (
            "⚙️ <b>Pengaturan Reminder</b>\n\n"
            f"• H-1: {'✅ Aktif' if prefs.reminder_h1 else '❌ Nonaktif'}\n"
            f"• Hari-H: {'✅ Aktif' if prefs.reminder_h0 else '❌ Nonaktif'}\n"
            f"• Jam kirim: {prefs.reminder_hour:02d}:00 WIB"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(("✅ " if prefs.reminder_h1 else "❌ ") + "Toggle H-1", callback_data="settings_toggle:reminder_h1"),
                    InlineKeyboardButton(("✅ " if prefs.reminder_h0 else "❌ ") + "Toggle Hari-H", callback_data="settings_toggle:reminder_h0"),
                ],
                [InlineKeyboardButton("🕒 Ganti Jam", callback_data="settings_hour")],
                [InlineKeyboardButton("⬅️ Kembali", callback_data="back_to_menu")],
            ]
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def settings_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        _, column = query.data.split(":", 1)
        prefs = self.storage.get_prefs(query.from_user.id)
        current = getattr(prefs, column)
        self.storage.update_reminder_flag(query.from_user.id, column, not current)
        await self.menu_settings(update, context)

    async def settings_hour(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        context.user_data["flow"] = {"name": "hour"}
        await query.edit_message_text(
            "🕒 Masukkan jam (0-23) untuk pengiriman reminder.",
            parse_mode=ParseMode.HTML,
        )

    # ------------------------------------------------------------------
    # text & document handler
    # ------------------------------------------------------------------
    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        flow = context.user_data.get("flow")
        if not flow:
            return
        name = flow.get("name")
        if name == "add":
            if flow.get("step") == "number":
                msisdn = normalize_number(update.message.text)
                if not msisdn:
                    await update.message.reply_text(
                        "Format nomor tidak valid. Contoh: <b>0819xxxxxxx</b>",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                flow.update({"step": "label", "msisdn": msisdn})
                await update.message.reply_text(
                    f"Bagus. Beri <b>label/nama</b> untuk <code>{msisdn}</code> (misal: <i>IWAN</i>).",
                    parse_mode=ParseMode.HTML,
                )
            elif flow.get("step") == "label":
                label = (update.message.text or "").strip()[:32] or "Customer"
                msisdn = flow.get("msisdn")
                ok, msg = self.storage.add_number(update.effective_user.id, label, msisdn)
                result = self.api.fetch(msisdn)
                if result.success:
                    self.storage.update_cache(msisdn, result.payload, None)
                else:
                    self.storage.update_cache(msisdn, result.payload, result.message, result.block_seconds)
                note = "✅ Data awal diambil." if result.success else f"⚠️ {html.escape(result.message)}"
                await update.message.reply_text(
                    f"{msg}\n{note}\n\nKembali ke menu utama:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._main_menu(update.effective_user.id),
                )
                context.user_data.pop("flow", None)
        elif name == "search":
            term = (update.message.text or "").strip()
            self.storage.update_search_query(update.effective_user.id, term)
            overview = self._overview_text(update.effective_user.id)
            await reply_in_chunks(update.message, overview, reply_markup=self._main_menu(update.effective_user.id), limit=self.config.message_chunk)
            context.user_data.pop("flow", None)
        elif name == "edit":
            msisdn = flow.get("msisdn")
            label = (update.message.text or "").strip()[:32]
            self.storage.update_label(update.effective_user.id, msisdn, label)
            await update.message.reply_text(
                "✅ Nama diperbarui.",
                parse_mode=ParseMode.HTML,
                reply_markup=self._main_menu(update.effective_user.id),
            )
            context.user_data.pop("flow", None)
        elif name == "hour":
            try:
                hour = int(update.message.text)
            except ValueError:
                await update.message.reply_text("Masukkan angka 0-23.")
                return
            hour = max(0, min(23, hour))
            self.storage.update_reminder_hour(update.effective_user.id, hour)
            await update.message.reply_text(
                f"Jam reminder diset ke {hour:02d}:00 WIB.",
                parse_mode=ParseMode.HTML,
                reply_markup=self._main_menu(update.effective_user.id),
            )
            context.user_data.pop("flow", None)

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        flow = context.user_data.get("flow")
        if not flow or flow.get("name") != "restore":
            return
        if not update.message or not update.message.document:
            await update.message.reply_text("❌ Harap kirim file .db / .db.gz.", parse_mode=ParseMode.HTML)
            return
        doc = update.message.document
        fn = doc.file_name or ""
        if not (fn.endswith(".db") or fn.endswith(".db.gz")):
            await update.message.reply_text("❌ Ekstensi tidak dikenal. Kirim .db atau .db.gz.", parse_mode=ParseMode.HTML)
            return
        self.config.backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(self.tz).strftime("%Y%m%d-%H%M%S")
        dl_path = self.config.backup_dir / f"upload-{ts}-{fn}"
        file = await doc.get_file()
        await file.download_to_drive(str(dl_path))
        target = dl_path
        if str(dl_path).endswith(".gz"):
            target = self.config.backup_dir / f"restore-{ts}.db"
            with gzip.open(dl_path, "rb") as f_in, open(target, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        shutil.copyfile(str(target), self.config.db_path)
        self.storage.migrate()
        context.user_data.pop("flow", None)
        await update.message.reply_text(
            "✅ Restore selesai.",
            parse_mode=ParseMode.HTML,
            reply_markup=self._main_menu(update.effective_user.id),
        )

    # ------------------------------------------------------------------
    # reminders & scheduler
    # ------------------------------------------------------------------
    async def reminder_job(self, app: Application) -> None:
        now = datetime.now(self.tz)
        today = now.date()
        con = self.storage.connect()
        cur = con.cursor()
        cur.execute(
            """
            SELECT tg_user_id, label, msisdn, last_payload, last_notified_expiry, last_notified_type
            FROM numbers
            ORDER BY tg_user_id, created_at ASC
            """
        )
        rows = cur.fetchall()
        con.close()
        for row in rows:
            tg_user_id = row["tg_user_id"]
            label = row["label"] or "Customer"
            msisdn = row["msisdn"]
            last_payload_json = row["last_payload"]
            last_notified_expiry = row["last_notified_expiry"] or ""
            last_notified_type = (row["last_notified_type"] or "").upper()
            prefs = self.storage.get_prefs(tg_user_id)
            if now.hour != prefs.reminder_hour or not last_payload_json:
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
            due_h1: list[dict[str, Any]] = []
            due_h0: list[dict[str, Any]] = []
            for pkg in packages:
                expiry_text = pkg.get("expiry")
                if not expiry_text or expiry_text == "-":
                    continue
                indicator = indicator_by_date(expiry_text)
                if indicator.days_left == 1 and prefs.reminder_h1:
                    due_h1.append(pkg)
                if indicator.days_left == 0 and prefs.reminder_h0:
                    due_h0.append(pkg)
            async def send(packages: List[dict[str, Any]], notif_type: str) -> None:
                if not packages:
                    return
                expiry_text = packages[0].get("expiry", "-")
                if notif_type == last_notified_type and last_notified_expiry == expiry_text:
                    return
                text = build_reminder_message(msisdn, label, notif_type, expiry_text, packages)
                await bot_send_in_chunks(app.bot, tg_user_id, text, limit=self.config.message_chunk)
                self.storage.set_last_notified(msisdn, expiry_text, notif_type)
            await send(due_h1, "H-1")
            await send(due_h0, "H0")

    async def scheduled_refresh(self, app: Application) -> None:
        now = int(time.time())
        con = self.storage.connect()
        cur = con.cursor()
        cur.execute("SELECT msisdn, COALESCE(last_fetch_ts,0), COALESCE(next_retry_ts,0) FROM numbers")
        rows = cur.fetchall()
        con.close()
        for msisdn, last_ts, next_retry in rows:
            last_ts = last_ts or 0
            next_retry = next_retry or 0
            if (now - last_ts) >= self.config.refresh_interval_seconds and now >= next_retry:
                result = self.api.fetch(msisdn)
                if result.success:
                    self.storage.update_cache(msisdn, result.payload, None)
                else:
                    self.storage.update_cache(msisdn, result.payload, result.message, result.block_seconds)

    async def weekly_backup_job(self, app: Application) -> None:
        await self._send_backup(app, "🗄 Backup mingguan database XL Reminder (otomatis).")

    def setup_scheduler(self, app: Application) -> None:
        self.scheduler.add_job(self.scheduled_refresh, "interval", minutes=30, args=[app], id="refresh_scan")
        self.scheduler.add_job(self.reminder_job, "cron", minute=0, args=[app], id="reminder_hourly")
        self.scheduler.add_job(
            self.weekly_backup_job,
            CronTrigger(day_of_week=self.config.weekly_backup_day, hour=self.config.weekly_backup_hour, minute=0),
            args=[app],
            id="weekly_backup",
        )
        self.scheduler.start()

    # ------------------------------------------------------------------
    # backup helper
    # ------------------------------------------------------------------
    async def _send_backup(self, app: Application, caption: str) -> None:
        self.config.backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(self.tz).strftime("%Y%m%d-%H%M%S")
        gz_path = self.config.backup_dir / f"backup-{ts}.db.gz"
        with open(self.config.db_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        targets = list(self.config.admin_ids)
        if not targets:
            con = self.storage.connect()
            cur = con.cursor()
            cur.execute("SELECT DISTINCT tg_user_id FROM users")
            targets = [row[0] for row in cur.fetchall()]
            con.close()
        for uid in targets:
            try:
                with open(gz_path, "rb") as f:
                    await app.bot.send_document(uid, InputFile(f, filename=gz_path.name), caption=caption)
            except Exception as exc:
                logging.error("Failed to send backup to %s: %s", uid, exc)

    # ------------------------------------------------------------------
    # error handler
    # ------------------------------------------------------------------
    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logging.exception("Exception while handling an update: %s", context.error)


__all__ = ["XLReminderApp"]
