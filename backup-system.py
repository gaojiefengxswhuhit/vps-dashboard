#!/usr/bin/env python3
"""Create a compact, restorable backup of the Azure SG control plane."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
BACKUP_DIR = Path("/var/backups/xushuo-control")
RETENTION = 14
IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.log",
    "*.bak",
    "*.bak_*",
    "*.bak-*",
    "venv",
    ".venv",
    "models",
    "model-cache",
)


def copy_item(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True, ignore=IGNORE)
    else:
        shutil.copy2(source, destination)
    return True


def sqlite_backup(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    return True


def write_crontab(destination: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/crontab", "-l"],
        text=True,
        capture_output=True,
        check=False,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(result.stdout if result.returncode == 0 else "", encoding="utf-8")


def prune() -> None:
    archives = sorted(BACKUP_DIR.glob("xushuo-control-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in archives[RETENTION:]:
        path.unlink(missing_ok=True)


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"xushuo-control-{stamp}.tar.gz"

    copied: list[str] = []
    with tempfile.TemporaryDirectory(prefix="xushuo-backup-") as temp_dir:
        stage = Path(temp_dir) / "xushuo-control"
        stage.mkdir(parents=True)

        sources = (
            (Path("/root/tg-node-bot"), stage / "root/tg-node-bot"),
            (Path("/opt/weixin-bridge"), stage / "opt/weixin-bridge"),
            (Path("/root/vps-status"), stage / "root/vps-status"),
            (Path("/etc/caddy"), stage / "etc/caddy"),
            (Path("/opt/xushuo-control"), stage / "opt/xushuo-control"),
            (Path("/etc/hk39-watch/config.json"), stage / "etc/hk39-watch/config.json"),
            (Path("/opt/marzban"), stage / "opt/marzban"),
            (Path("/etc/marzban"), stage / "etc/marzban"),
            (Path("/opt/openlist/data/config.json"), stage / "opt/openlist/data/config.json"),
            (Path("/opt/openlist/data/settings.json"), stage / "opt/openlist/data/settings.json"),
        )
        for source, destination in sources:
            if copy_item(source, destination):
                copied.append(str(source))

        if sqlite_backup(Path("/var/lib/marzban/db.sqlite3"), stage / "var/lib/marzban/db.sqlite3"):
            copied.append("/var/lib/marzban/db.sqlite3")

        units = stage / "etc/systemd/system"
        units.mkdir(parents=True, exist_ok=True)
        for pattern in ("*weixin*.service", "*tg-node*.service", "*clipboard*.service", "*ai-agent*.service", "*jp-auto*.service", "*openlist*.service", "*cliproxy*.service", "*marzban*.service", "*xushuo*.service"):
            for source in Path("/etc/systemd/system").glob(pattern):
                if source.is_file():
                    shutil.copy2(source, units / source.name)
                    copied.append(str(source))

        write_crontab(stage / "root/root-crontab.txt")
        copied.append("root crontab")
        manifest = {
            "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "format": 1,
            "retention": RETENTION,
            "sources": sorted(set(copied)),
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        with tarfile.open(archive, "w:gz", compresslevel=6) as bundle:
            bundle.add(stage, arcname="xushuo-control", recursive=True)

    os.chmod(archive, 0o600)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not any(item.name == "xushuo-control/manifest.json" for item in members):
            archive.unlink(missing_ok=True)
            raise RuntimeError("backup verification failed: manifest missing")
    prune()
    print(json.dumps({
        "success": True,
        "archive": str(archive),
        "size": archive.stat().st_size,
        "sources": len(set(copied)),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
