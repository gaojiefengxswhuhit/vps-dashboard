#!/usr/bin/env python3
"""Private file-drop controls plus public tokenized downloads and webhooks."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


HOST = "127.0.0.1"
PORT = 8791
TZ = ZoneInfo("Asia/Shanghai")
STATE_DIR = Path("/var/lib/xushuo-tools")
DB_PATH = STATE_DIR / "tools.db"
FILE_DIR = Path("/srv/xushuo-drop/files")
WEIXIN_NOTIFY = Path("/usr/local/bin/weixin-notify")
TELEGRAM_CONFIG = Path("/root/tg-node-bot/config.json")
PUBLIC_ORIGIN = "https://status.xushuo.uk"
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
MIN_FREE_AFTER_UPLOAD = 2 * 1024 * 1024 * 1024
HOOK_MAX_BODY = 1024 * 1024
HOOK_RATE_LIMIT = 30
RATE_WINDOW = 60
RATE_STATE: dict[str, list[float]] = {}
RATE_LOCK = threading.Lock()


def now() -> datetime:
    return datetime.now(TZ)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    os.chmod(FILE_DIR.parent, 0o700)
    os.chmod(FILE_DIR, 0o700)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                stored_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                max_downloads INTEGER NOT NULL,
                downloads INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS webhooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                channels TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_at TEXT,
                hits INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                webhook_id INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                title TEXT NOT NULL,
                preview TEXT NOT NULL,
                delivered INTEGER NOT NULL,
                detail TEXT NOT NULL,
                FOREIGN KEY(webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
            );
            """
        )
        for name, source in (("GitHub", "github"), ("Discord", "discord"), ("邮箱", "email")):
            exists = db.execute("SELECT 1 FROM webhooks WHERE source=?", (source,)).fetchone()
            if not exists:
                db.execute(
                    "INSERT INTO webhooks(name,source,token,channels,created_at) VALUES(?,?,?,?,?)",
                    (name, source, secrets.token_urlsafe(24), json.dumps(["wechat"]), iso()),
                )


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ)


def cleanup_expired() -> int:
    removed = 0
    with connect() as db:
        rows = db.execute("SELECT id,stored_name FROM files WHERE status='active' AND expires_at<=?", (iso(),)).fetchall()
        for row in rows:
            (FILE_DIR / row["stored_name"]).unlink(missing_ok=True)
            db.execute("UPDATE files SET status='expired' WHERE id=?", (row["id"],))
            removed += 1
    return removed


def cleanup_loop() -> None:
    while True:
        time.sleep(10 * 60)
        try:
            cleanup_expired()
        except Exception as exc:
            print(f"cleanup failed: {type(exc).__name__}: {exc}", flush=True)


def clean_filename(value: str) -> str:
    name = Path(urllib.parse.unquote(value or "upload.bin")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    return (name or "upload.bin")[:180]


def file_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["original_name"],
        "size": row["size"],
        "mime": row["mime"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "max_downloads": row["max_downloads"],
        "downloads": row["downloads"],
        "status": row["status"],
        "url": f"{PUBLIC_ORIGIN}/d/{row['token']}/{urllib.parse.quote(row['original_name'])}",
    }


def webhook_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "source": row["source"],
        "channels": json.loads(row["channels"]),
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "last_at": row["last_at"],
        "hits": row["hits"],
        "url": f"{PUBLIC_ORIGIN}/hook/{row['token']}",
    }


def load_telegram_config() -> tuple[str | None, list[str]]:
    try:
        cfg = json.loads(TELEGRAM_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return None, []
    token = cfg.get("bot_token") or cfg.get("token") or cfg.get("TELEGRAM_BOT_TOKEN")
    raw = cfg.get("chat_ids") or cfg.get("chat_id") or []
    ids = raw if isinstance(raw, list) else [raw]
    return str(token) if token else None, [str(value) for value in ids if str(value).strip()]


def send_telegram(text: str) -> tuple[bool, str]:
    token, chat_ids = load_telegram_config()
    if not token or not chat_ids:
        return False, "Telegram 配置缺失"
    for chat_id in chat_ids:
        body = json.dumps({"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    return True, "Telegram 已发送"


def deliver(text: str, channels: list[str]) -> tuple[bool, str]:
    results = []
    ok = True
    if "wechat" in channels:
        result = subprocess.run(
            [str(WEIXIN_NOTIFY)],
            input=text,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        channel_ok = result.returncode == 0
        ok = ok and channel_ok
        results.append("微信已入队" if channel_ok else f"微信失败：{(result.stderr or '')[-120:]}")
    if "telegram" in channels:
        try:
            channel_ok, detail = send_telegram(text)
        except Exception as exc:
            channel_ok, detail = False, f"{type(exc).__name__}: {exc}"
        ok = ok and channel_ok
        results.append(detail)
    return ok, "；".join(results) or "未选择通知渠道"


def rate_allowed(address: str) -> bool:
    current = time.time()
    with RATE_LOCK:
        rows = [stamp for stamp in RATE_STATE.get(address, []) if current - stamp < RATE_WINDOW]
        if len(rows) >= HOOK_RATE_LIMIT:
            RATE_STATE[address] = rows
            return False
        rows.append(current)
        RATE_STATE[address] = rows
        return True


def compact(value, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def format_webhook(source: str, payload: dict, headers) -> tuple[str, str]:
    if source == "github":
        event = headers.get("X-GitHub-Event") or payload.get("event") or "event"
        repo = compact((payload.get("repository") or {}).get("full_name") or payload.get("repository") or "GitHub")
        sender = compact((payload.get("sender") or {}).get("login") or payload.get("sender") or "unknown")
        action = compact(payload.get("action") or event)
        if event == "push":
            branch = compact(str(payload.get("ref") or "").replace("refs/heads/", ""))
            count = len(payload.get("commits") or [])
            title = f"GitHub · {repo} 推送"
            text = f"📨 {title}\n分支：{branch or '--'}\n提交：{count} 个\n发起：{sender}"
        else:
            title = f"GitHub · {repo} {event}"
            text = f"📨 {title}\n动作：{action}\n发起：{sender}"
        return title, text
    if source == "discord":
        author = payload.get("author") or payload.get("user") or {}
        author_name = compact(author.get("username") if isinstance(author, dict) else author) or "Discord"
        content = compact(payload.get("content") or payload.get("message") or payload.get("text") or payload)
        title = f"Discord · {author_name}"
        return title, f"💬 {title}\n{content or '收到一条新事件'}"
    if source == "email":
        sender = compact(payload.get("from") or payload.get("sender") or "未知发件人")
        subject = compact(payload.get("subject") or payload.get("title") or "新邮件")
        content = compact(payload.get("text") or payload.get("body") or payload.get("preview"), 360)
        title = f"邮件 · {subject}"
        return title, f"✉️ {title}\n来自：{sender}\n{content}".strip()
    title = compact(payload.get("title") or payload.get("event") or "Webhook 事件")
    content = compact(payload.get("message") or payload.get("text") or payload.get("content") or payload, 500)
    return title, f"🔔 {title}\n{content}".strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "xushuo-tools/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.client_address[0]} [{iso()}] {fmt % args}", flush=True)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, maximum: int = HOOK_MAX_BODY) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > maximum:
            raise ValueError("请求体大小无效")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"content": value}
        except Exception:
            return {"text": raw.decode("utf-8", "replace")}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, {"success": True, "time": iso()})
            return
        if parsed.path in {"/drop-api/status", "/tool-api/status"}:
            cleanup_expired()
            with connect() as db:
                files = [file_payload(row) for row in db.execute("SELECT * FROM files ORDER BY id DESC LIMIT 100").fetchall()]
                hooks = [webhook_payload(row) for row in db.execute("SELECT * FROM webhooks ORDER BY id").fetchall()]
                events = [dict(row) for row in db.execute("SELECT * FROM webhook_events ORDER BY id DESC LIMIT 20").fetchall()]
            disk = shutil.disk_usage(FILE_DIR)
            self.send_json(200, {
                "success": True,
                "server_time": iso(),
                "drop": {
                    "files": files,
                    "max_upload": MAX_UPLOAD,
                    "disk_free": disk.free,
                    "disk_total": disk.total,
                },
                "webhooks": {"endpoints": hooks, "events": events},
            })
            return
        if parsed.path.startswith("/d/"):
            self.download_file(parsed.path)
            return
        self.send_json(404, {"success": False, "message": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/drop-api/upload":
            self.upload_file(parsed)
            return
        if parsed.path == "/drop-api/delete":
            self.delete_file()
            return
        if parsed.path == "/webhook-api/create":
            self.create_webhook()
            return
        if parsed.path == "/webhook-api/delete":
            self.delete_webhook()
            return
        if parsed.path == "/webhook-api/test":
            self.test_webhook()
            return
        if parsed.path.startswith("/hook/"):
            self.receive_webhook(parsed.path)
            return
        self.send_json(404, {"success": False, "message": "not found"})

    def upload_file(self, parsed) -> None:
        cleanup_expired()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            self.send_json(400, {"success": False, "message": "没有收到文件"})
            return
        if length > MAX_UPLOAD:
            self.send_json(413, {"success": False, "message": "文件超过 2 GB 上限"})
            return
        disk = shutil.disk_usage(FILE_DIR)
        if disk.free - length < MIN_FREE_AFTER_UPLOAD:
            self.send_json(507, {"success": False, "message": "磁盘剩余空间不足"})
            return
        query = urllib.parse.parse_qs(parsed.query)
        hours = max(1, min(168, int((query.get("hours") or [24])[0])))
        downloads = max(1, min(20, int((query.get("downloads") or [1])[0])))
        original = clean_filename(self.headers.get("X-File-Name") or "upload.bin")
        token = secrets.token_urlsafe(18)
        stored = f"{int(time.time())}-{secrets.token_hex(10)}.bin"
        destination = FILE_DIR / stored
        temp = destination.with_suffix(".part")
        remaining = length
        digest = hashlib.sha256()
        try:
            with temp.open("wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise IOError("上传连接提前中断")
                    handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            os.chmod(temp, 0o600)
            temp.replace(destination)
            created = now()
            expires = created + timedelta(hours=hours)
            mime = self.headers.get("Content-Type") or mimetypes.guess_type(original)[0] or "application/octet-stream"
            with connect() as db:
                cursor = db.execute(
                    "INSERT INTO files(token,stored_name,original_name,size,mime,created_at,expires_at,max_downloads) VALUES(?,?,?,?,?,?,?,?)",
                    (token, stored, original, length, mime, iso(created), iso(expires), downloads),
                )
                row = db.execute("SELECT * FROM files WHERE id=?", (cursor.lastrowid,)).fetchone()
            payload = file_payload(row)
            payload["sha256"] = digest.hexdigest()
            self.send_json(201, {"success": True, "message": "限时链接已创建", "file": payload})
        except Exception as exc:
            temp.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            self.send_json(500, {"success": False, "message": f"上传失败：{type(exc).__name__}"})

    def delete_file(self) -> None:
        try:
            payload = self.read_json(8192)
            file_id = int(payload.get("id") or 0)
        except Exception:
            self.send_json(400, {"success": False, "message": "文件编号无效"})
            return
        with connect() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if not row:
                self.send_json(404, {"success": False, "message": "文件不存在"})
                return
            (FILE_DIR / row["stored_name"]).unlink(missing_ok=True)
            db.execute("UPDATE files SET status='deleted' WHERE id=?", (file_id,))
        self.send_json(200, {"success": True, "message": "文件和链接已删除"})

    def download_file(self, path: str) -> None:
        token = urllib.parse.unquote(path.split("/")[2] if len(path.split("/")) > 2 else "")
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
            if not row or row["status"] != "active":
                self.send_json(404, {"success": False, "message": "链接不存在或已失效"})
                return
            if parse_iso(row["expires_at"]) <= now() or row["downloads"] >= row["max_downloads"]:
                (FILE_DIR / row["stored_name"]).unlink(missing_ok=True)
                db.execute("UPDATE files SET status='expired' WHERE id=?", (row["id"],))
                self.send_json(410, {"success": False, "message": "链接已过期或下载次数已用完"})
                return
            source = FILE_DIR / row["stored_name"]
            if not source.exists():
                db.execute("UPDATE files SET status='missing' WHERE id=?", (row["id"],))
                self.send_json(410, {"success": False, "message": "文件已被清理"})
                return
            new_count = row["downloads"] + 1
            db.execute("UPDATE files SET downloads=? WHERE id=?", (new_count, row["id"]))

        encoded = urllib.parse.quote(row["original_name"])
        self.send_response(200)
        self.send_header("Content-Type", row["mime"])
        self.send_header("Content-Length", str(row["size"]))
        self.send_header("Content-Disposition", f"attachment; filename=download; filename*=UTF-8''{encoded}")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            with source.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            if new_count >= row["max_downloads"]:
                source.unlink(missing_ok=True)
                with connect() as db:
                    db.execute("UPDATE files SET status='consumed' WHERE id=?", (row["id"],))

    def create_webhook(self) -> None:
        try:
            payload = self.read_json(16384)
            name = compact(payload.get("name") or "自定义 Webhook", 40)
            source = compact(payload.get("source") or "general", 24).lower()
            channels = [item for item in payload.get("channels", ["wechat"]) if item in {"wechat", "telegram"}]
            if not channels:
                channels = ["wechat"]
        except Exception as exc:
            self.send_json(400, {"success": False, "message": str(exc)})
            return
        with connect() as db:
            cursor = db.execute(
                "INSERT INTO webhooks(name,source,token,channels,created_at) VALUES(?,?,?,?,?)",
                (name, source, secrets.token_urlsafe(24), json.dumps(channels), iso()),
            )
            row = db.execute("SELECT * FROM webhooks WHERE id=?", (cursor.lastrowid,)).fetchone()
        self.send_json(201, {"success": True, "message": "Webhook 已创建", "webhook": webhook_payload(row)})

    def delete_webhook(self) -> None:
        try:
            payload = self.read_json(8192)
            webhook_id = int(payload.get("id") or 0)
        except Exception:
            self.send_json(400, {"success": False, "message": "Webhook 编号无效"})
            return
        with connect() as db:
            changed = db.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,)).rowcount
        self.send_json(200 if changed else 404, {"success": bool(changed), "message": "Webhook 已删除" if changed else "Webhook 不存在"})

    def test_webhook(self) -> None:
        try:
            payload = self.read_json(8192)
            webhook_id = int(payload.get("id") or 0)
        except Exception:
            self.send_json(400, {"success": False, "message": "Webhook 编号无效"})
            return
        with connect() as db:
            row = db.execute("SELECT * FROM webhooks WHERE id=?", (webhook_id,)).fetchone()
        if not row:
            self.send_json(404, {"success": False, "message": "Webhook 不存在"})
            return
        title = f"{row['name']} 测试事件"
        ok, detail = deliver(f"🔔 {title}\n时间：{iso()}", json.loads(row["channels"]))
        self.send_json(200 if ok else 500, {"success": ok, "message": "测试事件已发送" if ok else "测试事件发送失败", "detail": detail})

    def receive_webhook(self, path: str) -> None:
        forwarded = self.headers.get("X-Forwarded-For") or self.client_address[0]
        address = forwarded.split(",", 1)[0].strip()
        if not rate_allowed(address):
            self.send_json(429, {"success": False, "message": "请求过于频繁"})
            return
        token = urllib.parse.unquote(path.split("/")[2] if len(path.split("/")) > 2 else "")
        with connect() as db:
            row = db.execute("SELECT * FROM webhooks WHERE token=? AND active=1", (token,)).fetchone()
        if not row:
            self.send_json(404, {"success": False, "message": "Webhook 不存在"})
            return
        try:
            payload = self.read_json()
            title, text = format_webhook(row["source"], payload, self.headers)
            ok, detail = deliver(text, json.loads(row["channels"]))
        except Exception as exc:
            title, text = "Webhook 处理失败", ""
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        with connect() as db:
            db.execute("UPDATE webhooks SET last_at=?,hits=hits+1 WHERE id=?", (iso(), row["id"]))
            db.execute(
                "INSERT INTO webhook_events(webhook_id,received_at,title,preview,delivered,detail) VALUES(?,?,?,?,?,?)",
                (row["id"], iso(), title[:160], compact(text, 300), int(ok), detail[:300]),
            )
            db.execute("DELETE FROM webhook_events WHERE id NOT IN (SELECT id FROM webhook_events ORDER BY id DESC LIMIT 200)")
        self.send_json(202 if ok else 502, {"success": ok, "message": "事件已转发" if ok else "事件接收成功，但通知失败", "detail": detail})


def main() -> None:
    init_db()
    cleanup_expired()
    threading.Thread(target=cleanup_loop, name="file-cleanup", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"xushuo tools API listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
