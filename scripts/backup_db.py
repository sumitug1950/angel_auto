"""Backs up the SQLite trade journal (data_store/angel_auto.db) to a timestamped copy in
data_store/backups/. Uses sqlite3's own backup API rather than a plain file copy, so it's
safe to run while the app is live (won't grab a half-written page mid-transaction).

Run manually, or on a cron/scheduled task (e.g. daily after square-off):
    .venv\\Scripts\\python.exe scripts\\backup_db.py
    .venv\\Scripts\\python.exe scripts\\backup_db.py --keep 30   # prune older backups
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from angel_auto.settings import get_settings

BACKUP_DIR = Path("data_store/backups")


def backup_sqlite_db(db_path: Path, keep: int | None) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found at {db_path}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # microseconds: avoids same-second collisions
    dest = BACKUP_DIR / f"angel_auto_{timestamp}.db"

    source_conn = sqlite3.connect(str(db_path))
    dest_conn = sqlite3.connect(str(dest))
    with dest_conn:
        source_conn.backup(dest_conn)
    source_conn.close()
    dest_conn.close()

    if keep is not None:
        _prune_old_backups(keep)

    return dest


def _prune_old_backups(keep: int) -> None:
    backups = sorted(BACKUP_DIR.glob("angel_auto_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in backups[keep:]:
        stale.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=None, help="prune backups beyond this count, newest kept")
    args = parser.parse_args()

    settings = get_settings()
    db_url = settings.app.database.url
    if not db_url.startswith("sqlite:///"):
        print(f"Skipping: backup_db.py only handles sqlite URLs, got {db_url!r}")
        return 1

    db_path = Path(db_url.removeprefix("sqlite:///"))
    dest = backup_sqlite_db(db_path, args.keep)
    print(f"Backed up {db_path} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
