#!/usr/bin/env python3
"""Authenticated, allowlisted control API for the xushuo operations console."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


HOST = "127.0.0.1"
PORT = 8790
TZ = ZoneInfo("Asia/Shanghai")
BOT_DIR = Path("/root/tg-node-bot")
WATCHDOG_STATE = BOT_DIR / "service_watchdog_state.json"
JP_STATE = BOT_DIR / "jp-auto-switcher-state.json"
JP_CONFIG = BOT_DIR / "jp-auto-switcher-config.json"
JP_LOCK = Path("/tmp/jp_auto_switcher.lock")
STATUS_SCRIPT = Path("/root/vps-status/publish_status.py")
EVENT_FILE = Path("/var/lib/xushuo-control/events.json")

SERVICE_SPECS = {
    "tg-node-bot.service": "Telegram Bot",
    "weixin-bridge.service": "微信 Bot",
    "weixin-push-worker.service": "微信推送",
    "clipboard-reminder.service": "收藏提醒",
    "ai-agent.service": "AI 服务",
    "jp-auto-switcher.service": "Japan Auto",
    "openlist.service": "OpenList",
    "cliproxyapi.service": "CPA",
    "marzban-node.service": "Marzban Node",
}

ACTION_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def command(args: list[str], timeout: int = 45) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
        output = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, output[-12000:]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def systemd_active(unit: str) -> bool:
    return subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def record_event(action: str, target: str, ok: bool, detail: str) -> None:
    events = load_json(EVENT_FILE, [])
    if not isinstance(events, list):
        events = []
    events.append({
        "at": now_iso(),
        "action": action,
        "target": target,
        "ok": bool(ok),
        "detail": detail[:300],
    })
    atomic_json(EVENT_FILE, events[-100:])


def sanitized_japan_auto() -> dict:
    state = load_json(JP_STATE)
    config = load_json(JP_CONFIG)
    backends = []
    health = state.get("backend_health") or {}
    for item in config.get("backends") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        backends.append({
            "name": name,
            "label": str(item.get("label") or name),
            "ip": str(item.get("ip") or ""),
            "priority": int(item.get("priority") or 50),
            "healthy": bool((health.get(name) or {}).get("ok")),
            "current": name == state.get("chosen"),
        })
    accounts = []
    thresholds = state.get("thresholds") or {}
    for account in config.get("accounts") or []:
        name = str(account.get("name") or "")
        usage = thresholds.get(name) or {}
        quota = float(account.get("quota_gb") or config.get("quota_gb") or 100)
        used = float(usage.get("used_gb") or 0)
        accounts.append({
            "name": name,
            "label": str(account.get("label") or name),
            "used_gb": round(used, 2),
            "threshold_gb": float(usage.get("threshold_gb") or config.get("switch_at_gb") or 80),
            "quota_gb": quota,
            "percent": round((used / quota * 100) if quota else 0, 1),
            "over": bool(usage.get("over")),
        })
    manual = state.get("manual_override") or {}
    return {
        "updated_at": state.get("updated_at"),
        "chosen": state.get("chosen"),
        "reason": state.get("reason") or "",
        "disabled": bool(state.get("disabled")),
        "manual_override": {
            "active": bool(manual.get("backend") and manual.get("until")),
            "backend": manual.get("backend"),
            "until": manual.get("until"),
        },
        "backends": sorted(backends, key=lambda item: item["priority"]),
        "accounts": accounts,
    }


def control_status() -> dict:
    watchdog = load_json(WATCHDOG_STATE)
    watchdog_services = watchdog.get("services") or {}
    services = []
    for unit, label in SERVICE_SPECS.items():
        state = watchdog_services.get(unit) or {}
        services.append({
            "unit": unit,
            "label": label,
            "active": systemd_active(unit),
            "healthy": bool(state.get("healthy", systemd_active(unit))),
            "status": state.get("status") or "unknown",
            "reason": state.get("reason") or "尚无探测详情",
            "checked_at": state.get("checked_at"),
        })
    events = load_json(EVENT_FILE, [])
    return {
        "success": True,
        "server_time": now_iso(),
        "watchdog": {
            "healthy": int(watchdog.get("healthy_count") or 0),
            "total": int(watchdog.get("total_count") or 0),
            "last_scan_at": watchdog.get("last_scan_at"),
        },
        "services": services,
        "japan_auto": sanitized_japan_auto(),
        "events": list(reversed(events[-12:])) if isinstance(events, list) else [],
    }


def clear_manual_override() -> tuple[bool, str]:
    JP_LOCK.touch(exist_ok=True)
    with JP_LOCK.open("r+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = load_json(JP_STATE)
        previous = (state.get("manual_override") or {}).get("backend")
        state["manual_override"] = {}
        atomic_json(JP_STATE, state)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    ok, output = command([
        "/usr/bin/flock", "-w", "30", str(JP_LOCK),
        "/usr/bin/python3", str(BOT_DIR / "jp_auto_switcher.py"),
    ], timeout=90)
    return ok, f"manual {previous or 'none'} -> auto" + (f"; {output[-300:]}" if not ok else "")


def run_action(payload: dict) -> tuple[int, dict]:
    action = str(payload.get("action") or "").strip()
    target = str(payload.get("target") or "").strip()

    with ACTION_LOCK:
        if action == "refresh_status":
            ok, detail = command(["/usr/bin/python3", str(STATUS_SCRIPT)], timeout=120)
            record_event(action, "fleet", ok, detail or "状态 JSON 已重新生成")
            return (200 if ok else 500), {"success": ok, "message": "状态数据已重新生成" if ok else "状态生成失败", "detail": detail[-1000:]}

        if action == "watchdog_scan":
            ok, detail = command(["/usr/bin/python3", str(BOT_DIR / "service_watchdog.py"), "--once"], timeout=90)
            record_event(action, "services", ok, detail or "自愈检查完成")
            return (200 if ok else 500), {"success": ok, "message": "自愈检查已完成" if ok else "自愈检查失败", "detail": detail[-2000:]}

        if action == "restart_service":
            if target not in SERVICE_SPECS:
                return 400, {"success": False, "message": "该服务不在重启白名单"}
            ok, detail = command(["/usr/bin/systemctl", "restart", target], timeout=45)
            if ok:
                ok = systemd_active(target)
                detail = "服务已恢复 active" if ok else "重启命令完成，但服务未 active"
            record_event(action, target, ok, detail)
            return (200 if ok else 500), {"success": ok, "message": f"{SERVICE_SPECS[target]} 已重启" if ok else f"{SERVICE_SPECS[target]} 重启失败", "detail": detail[-1000:]}

        if action == "switch_japan_auto":
            config = load_json(JP_CONFIG)
            valid = {str(item.get("name")) for item in config.get("backends") or []}
            if target not in valid:
                return 400, {"success": False, "message": "未知 Japan Auto 后端"}
            ok, detail = command([
                "/usr/bin/flock", "-w", "30", str(JP_LOCK),
                "/usr/bin/python3", str(BOT_DIR / "jp_auto_switcher.py"),
                "--force-backend", target,
                "--manual-hold-hours", "24",
            ], timeout=120)
            record_event(action, target, ok, "手动保持 24 小时" if ok else detail)
            return (200 if ok else 500), {"success": ok, "message": "Japan Auto 已切换并保持 24 小时" if ok else "Japan Auto 切换失败", "detail": detail[-1000:]}

        if action == "resume_japan_auto":
            ok, detail = clear_manual_override()
            record_event(action, "auto", ok, detail)
            return (200 if ok else 500), {"success": ok, "message": "Japan Auto 已恢复自动选择" if ok else "恢复自动选择失败", "detail": detail[-1000:]}

    return 400, {"success": False, "message": "未知操作"}


def service_logs(unit: str) -> tuple[int, dict]:
    if unit not in SERVICE_SPECS:
        return 400, {"success": False, "message": "该服务不在日志白名单"}
    if unit == "jp-auto-switcher.service":
        ok, output = command(["/usr/bin/tail", "-n", "100", str(BOT_DIR / "jp_auto_switcher.log")], timeout=8)
    else:
        ok, output = command(["/usr/bin/journalctl", "-u", unit, "-n", "100", "--no-pager", "-o", "short-iso"], timeout=12)
    return (200 if ok else 500), {"success": ok, "unit": unit, "label": SERVICE_SPECS[unit], "logs": output[-24000:]}


class Handler(BaseHTTPRequestHandler):
    server_version = "xushuo-control/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.client_address[0]} [{now_iso()}] {fmt % args}", flush=True)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.removeprefix("/control-api") or "/"
        if path == "/auth":
            self.send_response(302)
            self.send_header("Location", "/#control")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/status":
            self.send_json(200, control_status())
            return
        if path == "/logs":
            unit = str((parse_qs(parsed.query).get("unit") or [""])[0])
            status, payload = service_logs(unit)
            self.send_json(status, payload)
            return
        self.send_json(404, {"success": False, "message": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.removeprefix("/control-api") or "/"
        if path != "/action":
            self.send_json(404, {"success": False, "message": "not found"})
            return
        if self.headers.get("X-Control-Requested") != "1":
            self.send_json(403, {"success": False, "message": "missing control request header"})
            return
        if int(self.headers.get("Content-Length") or 0) > 8192:
            self.send_json(413, {"success": False, "message": "payload too large"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json(400, {"success": False, "message": "invalid json"})
            return
        status, result = run_action(payload)
        self.send_json(status, result)


def main() -> None:
    EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"xushuo control API listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
