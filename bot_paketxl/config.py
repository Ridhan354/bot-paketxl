"""Application configuration utilities."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class AppConfig:
    """Immutable container for runtime configuration."""

    bot_token: str
    api_template: str
    db_path: Path
    request_timeout: int
    refresh_interval_seconds: int
    default_reminder_hour: int
    admin_ids: set[int]
    backup_dir: Path
    weekly_backup_day: str
    weekly_backup_hour: int
    repo_url: str
    repo_branch: str
    install_base: Path
    app_dir: Path
    venv_path: Path
    git_bin: str
    timezone: str
    message_chunk: int

    @staticmethod
    def _parse_admin_ids(raw: str) -> set[int]:
        ids: set[int] = set()
        for token in raw.replace(",", " ").split():
            try:
                ids.add(int(token))
            except ValueError:
                continue
        return ids

    @classmethod
    def load(cls) -> "AppConfig":
        env = os.environ
        app_dir = Path(__file__).resolve().parent.parent / "app"
        install_base = Path(env.get("INSTALL_BASE", str(app_dir.parent)))
        db_path = Path(env.get("DB_PATH", str(install_base / "xl_reminder.db")))
        backup_dir = Path(env.get("BACKUP_DIR", str(install_base / "backups")))
        venv_path = Path(env.get("VENV_PATH", str(install_base / ".venv")))
        message_chunk = int(env.get("MESSAGE_CHUNK", env.get("CHUNK_SIZE", "3800")))

        return cls(
            bot_token=env.get("BOT_TOKEN", ""),
            api_template=env.get(
                "API_TEMPLATE",
                "https://bendith.my.id/end.php?check=package&number={number}&version=2",
            ),
            db_path=db_path,
            request_timeout=int(env.get("REQUEST_TIMEOUT", "12")),
            refresh_interval_seconds=int(env.get("REFRESH_INTERVAL_SECS", str(6 * 3600))),
            default_reminder_hour=int(env.get("REMINDER_HOUR", "9")),
            admin_ids=cls._parse_admin_ids(env.get("ADMIN_IDS", "")),
            backup_dir=backup_dir,
            weekly_backup_day=env.get("WEEKLY_BACKUP_DAY", "sun"),
            weekly_backup_hour=int(env.get("WEEKLY_BACKUP_HOUR", "2")),
            repo_url=env.get("REPO_URL", "https://github.com/Ridhan354/bot-paketxl.git"),
            repo_branch=env.get("REPO_BRANCH", "main"),
            install_base=install_base,
            app_dir=app_dir,
            venv_path=venv_path,
            git_bin=env.get("GIT_BIN", "git"),
            timezone=env.get("TIMEZONE", "Asia/Jakarta"),
            message_chunk=message_chunk,
        )

    def ensure_token(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN is required in the environment or .env file")
