"""Consistent copies of the SQLite database. Uses SQLite's own online backup, so it is safe while the app is running
(copying the file by hand is not). Keeps the newest BACKUP_KEEP copies in DATA_DIR/backups."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import config

log = logging.getLogger("banksview.backup")


def make_backup() -> str:
    out_dir = config.DATA_DIR / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"app-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(config.DB_PATH))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close(); src.close()
    if ok != "ok":
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed its integrity check: {ok}")
    for old in sorted(out_dir.glob("app-*.db"))[:-config.BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return str(dest)


def start_daily() -> None:
    """Background thread: one backup shortly after start and then every 24 hours."""
    def loop():
        time.sleep(60)
        while True:
            try:
                log.info("backup written: %s", make_backup())
            except Exception:
                log.exception("backup failed")
            time.sleep(24 * 3600)
    threading.Thread(target=loop, name="daily-backup", daemon=True).start()
