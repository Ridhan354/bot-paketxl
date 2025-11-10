"""Utilities for formatting bot messages."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Iterable, Optional
import html
import re


@dataclass(frozen=True)
class ExpiryIndicator:
    icon: str
    text: str
    days_left: Optional[int]


def normalize_number(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
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


def parse_expiry_text(expiry_text: str) -> Optional[date]:
    try:
        dt_naive = datetime.strptime(expiry_text, "%d-%m-%Y")
        return dt_naive.date()
    except Exception:
        return None


def indicator_by_date(expiry_text: str) -> ExpiryIndicator:
    dt = parse_expiry_text(expiry_text)
    if not dt:
        return ExpiryIndicator("⚪", "Tanggal tidak diketahui", None)
    today = date.today()
    delta = (dt - today).days
    if delta < 0:
        return ExpiryIndicator("🔴", f"Sudah lewat {abs(delta)} hari", delta)
    if delta == 0:
        return ExpiryIndicator("🟠", "HARI INI", delta)
    if delta == 1:
        return ExpiryIndicator("🟡", "Besok", delta)
    if delta <= 7:
        return ExpiryIndicator("🟢", f"{delta} hari lagi", delta)
    return ExpiryIndicator("🔵", f"{delta} hari lagi", delta)


def quotas_block(quotas: Iterable[dict[str, Any]], indent: str = "") -> str:
    lines: list[str] = []
    for quota in quotas:
        qname = quota.get("name", "-")
        bar = progress_bar(quota.get("percent"))
        remaining = nice_size(quota.get("remaining"))
        total = nice_size(quota.get("total"))
        lines.append(f"{indent}🔸 <b>{html.escape(qname)}</b>")
        lines.append(
            f"{indent}&nbsp;&nbsp;{bar} — sisa: <b>{html.escape(remaining)}</b> / {html.escape(total)}"
        )
    return "\n".join(lines)


def reminder_package_lines(packages: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
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


def primary_package_info(payload: dict[str, Any]) -> tuple[str, str, Optional[str]]:
    pk = payload.get("package_info") or {}
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
    return abbr, expiry, full_name
