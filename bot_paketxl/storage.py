"""SQLite storage helpers and domain models."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
import json
import sqlite3
import time


@dataclass
class NumberRecord:
    id: int
    tg_user_id: int
    label: str
    msisdn: str
    created_at: int
    last_fetch_ts: int
    last_payload: Optional[dict]
    next_retry_ts: int
    last_error: Optional[str]
    last_notified_expiry: Optional[str]
    last_notified_type: Optional[str]
    last_notified_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "NumberRecord":
        payload = json.loads(row["last_payload"]) if row["last_payload"] else None
        return cls(
            id=row["id"],
            tg_user_id=row["tg_user_id"],
            label=row["label"],
            msisdn=row["msisdn"],
            created_at=row["created_at"],
            last_fetch_ts=row["last_fetch_ts"] or 0,
            last_payload=payload,
            next_retry_ts=row["next_retry_ts"] or 0,
            last_error=row["last_error"],
            last_notified_expiry=row["last_notified_expiry"],
            last_notified_type=row["last_notified_type"],
            last_notified_at=row["last_notified_at"] or 0,
        )


@dataclass
class UserPreferences:
    tg_user_id: int
    sort_order: str = "asc"
    search_query: str = ""
    reminder_h1: bool = True
    reminder_h0: bool = True
    reminder_hour: int = 9


class Storage:
    """Thin wrapper on top of sqlite3 for domain persistence."""

    def __init__(self, db_path: Path, default_reminder_hour: int) -> None:
        self.db_path = Path(db_path)
        self.default_reminder_hour = default_reminder_hour
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------------
    # migrations
    # ------------------------------------------------------------------
    def migrate(self) -> None:
        con = self.connect()
        cur = con.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL UNIQUE,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL,
                label TEXT,
                msisdn TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_fetch_ts INTEGER DEFAULT 0,
                last_payload TEXT,
                next_retry_ts INTEGER DEFAULT 0,
                last_error TEXT,
                last_notified_expiry TEXT,
                last_notified_type TEXT,
                last_notified_at INTEGER DEFAULT 0,
                UNIQUE(msisdn)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_prefs (
                tg_user_id INTEGER PRIMARY KEY,
                sort_order TEXT NOT NULL DEFAULT 'asc',
                search_query TEXT DEFAULT '',
                reminder_h1 INTEGER NOT NULL DEFAULT 1,
                reminder_h0 INTEGER NOT NULL DEFAULT 1,
                reminder_hour INTEGER NOT NULL DEFAULT 9
            )
            """
        )

        # ensure reminder hour default aligns with config
        cur.execute(
            "UPDATE user_prefs SET reminder_hour=? WHERE reminder_hour IS NULL",
            (self.default_reminder_hour,),
        )

        con.commit()
        con.close()

    # ------------------------------------------------------------------
    # user helpers
    # ------------------------------------------------------------------
    def ensure_user(self, tg_user: Any) -> None:
        con = self.connect()
        cur = con.cursor()
        cur.execute("SELECT id FROM users WHERE tg_user_id=?", (tg_user.id,))
        if not cur.fetchone():
            cur.execute(
                """
                INSERT INTO users (tg_user_id, first_name, last_name, username, created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    tg_user.id,
                    tg_user.first_name,
                    tg_user.last_name,
                    tg_user.username,
                    int(time.time()),
                ),
            )
            con.commit()
        con.close()

    # ------------------------------------------------------------------
    # number helpers
    # ------------------------------------------------------------------
    def add_number(self, tg_user_id: int, label: str, msisdn: str) -> tuple[bool, str]:
        con = self.connect()
        cur = con.cursor()
        try:
            cur.execute(
                """
                INSERT INTO numbers (
                    tg_user_id, label, msisdn, is_default, created_at,
                    last_fetch_ts, last_payload, next_retry_ts, last_error,
                    last_notified_expiry, last_notified_type, last_notified_at
                )
                VALUES (?, ?, ?, 0, ?, 0, NULL, 0, NULL, NULL, NULL, 0)
                """,
                (tg_user_id, label, msisdn, int(time.time())),
            )
            con.commit()
            return True, "Nomor berhasil didaftarkan."
        except sqlite3.IntegrityError:
            return False, "Nomor sudah terdaftar."
        finally:
            con.close()

    def list_numbers(self, tg_user_id: int) -> list[NumberRecord]:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM numbers WHERE tg_user_id=? ORDER BY created_at ASC",
            (tg_user_id,),
        )
        rows = [NumberRecord.from_row(r) for r in cur.fetchall()]
        con.close()
        return rows

    def get_number(self, msisdn: str) -> Optional[NumberRecord]:
        con = self.connect()
        cur = con.cursor()
        cur.execute("SELECT * FROM numbers WHERE msisdn=?", (msisdn,))
        row = cur.fetchone()
        con.close()
        return NumberRecord.from_row(row) if row else None

    def update_label(self, tg_user_id: int, msisdn: str, new_label: str) -> int:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            "UPDATE numbers SET label=? WHERE tg_user_id=? AND msisdn=?",
            (new_label, tg_user_id, msisdn),
        )
        count = cur.rowcount
        con.commit()
        con.close()
        return count

    def delete_number(self, tg_user_id: int, msisdn: str) -> int:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            "DELETE FROM numbers WHERE tg_user_id=? AND msisdn=?",
            (tg_user_id, msisdn),
        )
        count = cur.rowcount
        con.commit()
        con.close()
        return count

    def update_cache(
        self,
        msisdn: str,
        payload: Optional[dict],
        error: Optional[str],
        block_for_seconds: int = 0,
    ) -> None:
        con = self.connect()
        cur = con.cursor()
        now = int(time.time())
        next_retry = now + block_for_seconds if block_for_seconds > 0 else 0
        cur.execute(
            """
            UPDATE numbers
            SET last_fetch_ts=?, last_payload=?, last_error=?, next_retry_ts=?
            WHERE msisdn=?
            """,
            (
                now,
                json.dumps(payload) if payload is not None else None,
                error,
                next_retry,
                msisdn,
            ),
        )
        con.commit()
        con.close()

    def set_last_notified(self, msisdn: str, expiry_text: str, notif_type: str) -> None:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            """
            UPDATE numbers
            SET last_notified_expiry=?, last_notified_type=?, last_notified_at=?
            WHERE msisdn=?
            """,
            (expiry_text, notif_type, int(time.time()), msisdn),
        )
        con.commit()
        con.close()

    # ------------------------------------------------------------------
    # cache helpers
    # ------------------------------------------------------------------
    def get_cached(self, msisdn: str) -> tuple[int, Optional[dict], Optional[str], int, Optional[str], Optional[str], int]:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            """
            SELECT last_fetch_ts, last_payload, last_error, next_retry_ts,
                   last_notified_expiry, last_notified_type, last_notified_at
            FROM numbers WHERE msisdn=?
            """,
            (msisdn,),
        )
        row = cur.fetchone()
        con.close()
        if not row:
            return 0, None, None, 0, None, None, 0
        payload = json.loads(row["last_payload"]) if row["last_payload"] else None
        return (
            row["last_fetch_ts"] or 0,
            payload,
            row["last_error"],
            row["next_retry_ts"] or 0,
            row["last_notified_expiry"],
            row["last_notified_type"],
            row["last_notified_at"] or 0,
        )

    # ------------------------------------------------------------------
    # preferences
    # ------------------------------------------------------------------
    def get_prefs(self, tg_user_id: int) -> UserPreferences:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM user_prefs WHERE tg_user_id=?",
            (tg_user_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO user_prefs (tg_user_id, reminder_hour) VALUES (?, ?)",
                (tg_user_id, self.default_reminder_hour),
            )
            con.commit()
            prefs = UserPreferences(
                tg_user_id=tg_user_id,
                reminder_hour=self.default_reminder_hour,
            )
        else:
            prefs = UserPreferences(
                tg_user_id=row["tg_user_id"],
                sort_order=row["sort_order"] or "asc",
                search_query=row["search_query"] or "",
                reminder_h1=bool(row["reminder_h1"] if row["reminder_h1"] is not None else 1),
                reminder_h0=bool(row["reminder_h0"] if row["reminder_h0"] is not None else 1),
                reminder_hour=row["reminder_hour"] if row["reminder_hour"] is not None else self.default_reminder_hour,
            )
        con.close()
        return prefs

    def update_sort_order(self, tg_user_id: int, sort_order: str) -> None:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO user_prefs (tg_user_id, sort_order)
            VALUES (?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET sort_order=excluded.sort_order
            """,
            (tg_user_id, sort_order),
        )
        con.commit()
        con.close()

    def update_search_query(self, tg_user_id: int, query: str) -> None:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO user_prefs (tg_user_id, search_query)
            VALUES (?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET search_query=excluded.search_query
            """,
            (tg_user_id, query),
        )
        con.commit()
        con.close()

    def clear_search(self, tg_user_id: int) -> None:
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            "UPDATE user_prefs SET search_query='' WHERE tg_user_id=?",
            (tg_user_id,),
        )
        con.commit()
        con.close()

    def update_reminder_flag(self, tg_user_id: int, column: str, value: bool) -> None:
        if column not in {"reminder_h1", "reminder_h0"}:
            raise ValueError("invalid reminder flag")
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            f"UPDATE user_prefs SET {column}=? WHERE tg_user_id=?",
            (1 if value else 0, tg_user_id),
        )
        con.commit()
        con.close()

    def update_reminder_hour(self, tg_user_id: int, hour: int) -> None:
        hour = max(0, min(23, int(hour)))
        con = self.connect()
        cur = con.cursor()
        cur.execute(
            "UPDATE user_prefs SET reminder_hour=? WHERE tg_user_id=?",
            (hour, tg_user_id),
        )
        con.commit()
        con.close()

    # ------------------------------------------------------------------
    # backups
    # ------------------------------------------------------------------
    def export_all_numbers(self) -> list[NumberRecord]:
        con = self.connect()
        cur = con.cursor()
        cur.execute("SELECT * FROM numbers ORDER BY created_at ASC")
        rows = [NumberRecord.from_row(r) for r in cur.fetchall()]
        con.close()
        return rows

    def bulk_insert_numbers(self, rows: Sequence[dict[str, Any]]) -> int:
        con = self.connect()
        cur = con.cursor()
        inserted = 0
        for row in rows:
            try:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO numbers (
                        tg_user_id, label, msisdn, created_at,
                        last_fetch_ts, last_payload, last_error,
                        next_retry_ts, last_notified_expiry, last_notified_type, last_notified_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("tg_user_id"),
                        row.get("label"),
                        row.get("msisdn"),
                        row.get("created_at", int(time.time())),
                        row.get("last_fetch_ts", 0),
                        json.dumps(row.get("last_payload")) if row.get("last_payload") is not None else None,
                        row.get("last_error"),
                        row.get("next_retry_ts", 0),
                        row.get("last_notified_expiry"),
                        row.get("last_notified_type"),
                        row.get("last_notified_at", 0),
                    ),
                )
                inserted += cur.rowcount
            except sqlite3.Error:
                continue
        con.commit()
        con.close()
        return inserted

    def set_multiple_cache(self, entries: Iterable[tuple[str, Optional[dict], Optional[str], int]]) -> None:
        con = self.connect()
        cur = con.cursor()
        now = int(time.time())
        for msisdn, payload, error, block in entries:
            next_retry = now + block if block > 0 else 0
            cur.execute(
                """
                UPDATE numbers
                SET last_fetch_ts=?, last_payload=?, last_error=?, next_retry_ts=?
                WHERE msisdn=?
                """,
                (
                    now,
                    json.dumps(payload) if payload is not None else None,
                    error,
                    next_retry,
                    msisdn,
                ),
            )
        con.commit()
        con.close()
