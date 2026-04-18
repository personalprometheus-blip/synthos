#!/usr/bin/env python3
"""
rotate_logs.py — Weekly log rotation for Synthos retail node
=============================================================
Rotates active logs, moves archives to logs/archive/,
compresses old archives, deletes archives older than 30 days.

Cron: 0 0 * * 0  python3 rotate_logs.py
"""
import os
import gzip
import shutil
from pathlib import Path
from datetime import datetime, timedelta

LOG_DIR = Path(__file__).resolve().parent.parent / 'logs'
ARCHIVE_DIR = LOG_DIR / 'archive'
ARCHIVE_DIR.mkdir(exist_ok=True)
DELETE_DAYS = 30

ACTIVE_LOGS = [
    'portal.log', 'scheduler.log', 'price_poller.log',
    'heartbeat.log', 'manual_run.log', 'watchdog.log',
    'market_daemon.log', 'boot.log', 'backup.log',
]

def rotate():
    now = datetime.now()
    stamp = now.strftime('%Y%m%d')
    rotated = compressed = deleted = 0

    # 1. Rotate active logs that have content
    for name in ACTIVE_LOGS:
        log_file = LOG_DIR / name
        if not log_file.exists():
            continue
        size = log_file.stat().st_size
        if size < 1024:
            continue
        archive_name = f"{name.replace('.log', '')}.{stamp}.log"
        archive_path = ARCHIVE_DIR / archive_name
        if archive_path.exists():
            continue
        shutil.move(str(log_file), str(archive_path))
        log_file.touch()
        rotated += 1
        print(f"Rotated: {name} ({size:,} bytes) → archive/{archive_name}")

    # 2. Compress uncompressed archives older than 1 day
    for f in ARCHIVE_DIR.glob('*.log'):
        if f.suffix == '.gz':
            continue
        age = (now - datetime.fromtimestamp(f.stat().st_mtime)).days
        if age >= 1:
            gz_path = f.with_suffix('.log.gz')
            with open(f, 'rb') as fin, gzip.open(gz_path, 'wb') as fout:
                shutil.copyfileobj(fin, fout)
            f.unlink()
            compressed += 1
            print(f"Compressed: {f.name} → {gz_path.name}")

    # 3. Delete archives older than DELETE_DAYS
    cutoff = now - timedelta(days=DELETE_DAYS)
    for f in list(ARCHIVE_DIR.glob('*.gz')) + list(ARCHIVE_DIR.glob('*.log')):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            deleted += 1
            print(f"Deleted (>{DELETE_DAYS}d): {f.name}")

    print(f"\nLog rotation complete: {rotated} rotated, {compressed} compressed, {deleted} deleted")

if __name__ == '__main__':
    rotate()
