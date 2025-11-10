"""High level text builders for Telegram responses."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional
import html
import time

from .formatting import (
    ExpiryIndicator,
    abbreviate_package,
    indicator_by_date,
    normalize_number,
    primary_package_info,
    quotas_block,
    reminder_package_lines,
)


def build_detail_message(payload: dict[str, Any]) -> str:
    subs = payload.get("subs_info", {}) or {}
    pk_info = payload.get("package_info", {}) or {}

    msisdn = subs.get("msisdn", "-")
    operator = subs.get("operator", "-")
    net_type = subs.get("net_type", "-")
    tenure = subs.get("tenure", "-")
    exp_date = subs.get("exp_date", "-")
    idv = subs.get("id_verified", "-")

    lines: list[str] = [
        f"📡 <b>Informasi Kuota — {html.escape(operator)}</b>",
        f"📱 Nomor: <code>{html.escape(msisdn)}</code>",
        f"📶 Jaringan: <b>{html.escape(net_type)}</b>  •  Validasi ID: <b>{html.escape(idv)}</b>",
        f"🕒 Masa Aktif: <b>{html.escape(exp_date)}</b>  •  Tenure: <b>{html.escape(tenure)}</b>",
        "",
    ]

    err_msg = (pk_info.get("error_message") or "").strip()
    if err_msg:
        lines.extend(
            [
                "🚫 <b>Pengecekan Ditolak</b>",
                f"🧭 Pesan: <i>{html.escape(err_msg)}</i>",
                "",
            ]
        )
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


def format_package(pkg: dict[str, Any], index: Optional[int] = None) -> str:
    name = pkg.get("name", "Unknown Package")
    expiry = pkg.get("expiry", "-")
    prefix = f"{index}. " if index is not None else ""
    header = f"{prefix}<b>{html.escape(name)}</b>"
    lines = [f"📦 {header}", f"⏳ Kedaluwarsa: <b>{html.escape(expiry)}</b>"]
    quota_block = quotas_block(pkg.get("quotas") or [], indent="&nbsp;" * (6 if index is not None else 2))
    if quota_block:
        lines.append(quota_block)
    return "\n".join(lines)


def build_overview_entry(
    label: str,
    msisdn: str,
    indicator: ExpiryIndicator,
    primary_package: str,
    expiry_text: str,
    quotas_line: str,
    last_fetch_ts: int,
    error: Optional[str] = None,
    blocked_until: Optional[int] = None,
) -> str:
    label_html = html.escape(label or msisdn)
    msisdn_html = html.escape(msisdn)
    lines = [
        f"⚪ 👤 <b>{label_html}</b>",
        f"📱 <code>{msisdn_html}</code>",
    ]
    if error:
        lines.extend(
            [
                f"🚫 <i>{html.escape(error)}</i>",
            ]
        )
        if blocked_until:
            wait_min = max(0, int((blocked_until - int(time.time())) / 60))
            lines.append(f"⏳ Coba lagi dalam ~{wait_min} menit")
    else:
        lines.extend(
            [
                f"{indicator.icon} {html.escape(primary_package)}",  # package label
                f"🗓️ Masa aktif paket: <b>{html.escape(expiry_text or '-')}</b>",
            ]
        )
        if quotas_line:
            lines.append(quotas_line)
        if last_fetch_ts:
            last_sync = datetime.fromtimestamp(last_fetch_ts).strftime("%d %b %Y %H:%M")
            lines.append(f"⏱️ Terakhir di-refresh: <i>{last_sync}</i>")
        else:
            lines.append("⚠️ Belum ada data. Gunakan menu cek untuk memuat.")
    return "\n".join(lines)


def build_overview_message(sections: Iterable[str]) -> str:
    return "\n\n".join(sections)


def build_reminder_message(
    msisdn: str,
    label: str,
    notif_type: str,
    expiry_text: str,
    packages: list[dict[str, Any]],
) -> str:
    indicator = indicator_by_date(expiry_text)
    header = "🔔 <b>Pengingat Paket</b>" if notif_type == "H0" else "⏰ <b>Pengingat H-1</b>"
    lines = [
        header,
        f"👤 {html.escape(label)}",
        f"📱 <code>{html.escape(msisdn)}</code>",
        f"{indicator.icon} Kedaluwarsa: <b>{html.escape(expiry_text)}</b> ({indicator.text})",
        "",
        *reminder_package_lines(packages),
    ]
    return "\n".join(lines)


__all__ = [
    "normalize_number",
    "build_detail_message",
    "build_overview_entry",
    "build_overview_message",
    "build_reminder_message",
    "abbreviate_package",
    "indicator_by_date",
    "primary_package_info",
]
