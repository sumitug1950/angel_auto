import sqlite3
from pathlib import Path

from scripts.backup_db import backup_sqlite_db


def test_backup_creates_a_copy(tmp_path, monkeypatch):
    import scripts.backup_db as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")

    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    dest = backup_sqlite_db(db_path, keep=None)

    assert dest.exists()
    copy_conn = sqlite3.connect(str(dest))
    assert copy_conn.execute("SELECT x FROM t").fetchone() == (42,)
    copy_conn.close()


def test_backup_prunes_old_backups(tmp_path, monkeypatch):
    import time

    import scripts.backup_db as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")

    db_path = tmp_path / "source.db"
    sqlite3.connect(str(db_path)).close()

    for _ in range(5):
        backup_sqlite_db(db_path, keep=None)
        time.sleep(0.01)  # ensure distinct mtimes for prune ordering

    backup_sqlite_db(db_path, keep=3)

    remaining = list((tmp_path / "backups").glob("angel_auto_*.db"))
    assert len(remaining) == 3


def test_backup_raises_when_source_missing(tmp_path, monkeypatch):
    import scripts.backup_db as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")
    try:
        backup_sqlite_db(tmp_path / "does_not_exist.db", keep=None)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
