#!/usr/bin/env python3
"""Authenticated, allowlisted control API for the xushuo operations console."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import urllib.request
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
BACKUP_DIR = Path("/var/backups/xushuo-control")
BACKUP_SCRIPT = Path("/opt/xushuo-control/backup-system.py")
CRONTAB = Path("/usr/bin/crontab")
TELEGRAM_CONFIG = BOT_DIR / "config.json"
EMAIL_CONFIG = Path("/etc/hk39-watch/config.json")
EMAIL_NOTIFY = BOT_DIR / "email_notify.py"
WEIXIN_NOTIFY = Path("/usr/local/bin/weixin-notify")
WEIXIN_ROOT = Path("/var/lib/weixin-push")

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

TASK_SPECS = {
    "market_alert": {
        "label": "行情与基金监控",
        "marker": "# market_alert",
        "schedule": "每分钟",
        "log": BOT_DIR / "market_alert.log",
        "run": ["/usr/bin/flock", "-n", "/tmp/market_alert.lock", "/usr/bin/timeout", "55", "/usr/bin/python3", "-u", str(BOT_DIR / "market_alert.py")],
    },
    "azure_traffic": {
        "label": "Azure 出站流量",
        "marker": "# azure_monitor_traffic_alert",
        "schedule": "每 10 分钟",
        "log": BOT_DIR / "azure_traffic_cache.json",
        "run": ["/usr/bin/python3", str(BOT_DIR / "azure_monitor_traffic.py"), "--alert"],
    },
    "status_watch": {
        "label": "机器掉线通知",
        "marker": "# vps_json_watch_email",
        "schedule": "每 2 分钟",
        "log": Path("/var/log/hk39-watch.log"),
        "run": ["/usr/bin/flock", "-n", "/tmp/vps_json_watch.lock", "/usr/bin/timeout", "55", "/usr/bin/python3", "/opt/hk39-watch/hk39_watch.py"],
    },
    "status_json": {
        "label": "VPS 状态采集",
        "marker": "# vps_status_json",
        "schedule": "每分钟",
        "log": Path("/root/vps-status/publish_status.log"),
        "run": ["/usr/bin/flock", "-n", "/tmp/vps_status.lock", "/usr/bin/timeout", "55", "/usr/bin/python3", str(STATUS_SCRIPT)],
    },
    "daily_briefing": {
        "label": "每日晨报",
        "marker": "# daily_briefing",
        "schedule": "每天 08:15",
        "log": BOT_DIR / "daily_briefing.log",
        "run": ["/usr/bin/flock", "-n", "/tmp/daily_briefing.lock", "/usr/bin/timeout", "240", "/usr/bin/python3", str(BOT_DIR / "daily_briefing.py")],
    },
    "ip_sentinel": {
        "label": "IP 净化检测",
        "marker": "# ip_sentinel runner",
        "schedule": "每 20 分钟",
        "log": Path("/opt/ip_sentinel/logs"),
        "run": ["/bin/bash", "/opt/ip_sentinel/core/runner.sh"],
    },
    "ip_sentinel_update": {
        "label": "IP 净化更新",
        "marker": "# ip_sentinel updater",
        "schedule": "每天 03:37",
        "log": Path("/opt/ip_sentinel"),
        "run": ["/bin/bash", "/opt/ip_sentinel/core/updater.sh"],
    },
    "system_backup": {
        "label": "控制中枢备份",
        "marker": "# xushuo_control_backup",
        "schedule": "每天 09:25",
        "log": BACKUP_DIR,
        "run": ["/usr/bin/python3", str(BACKUP_SCRIPT)],
    },
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


def command_input(args: list[str], body: str, timeout: int = 45) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            args,
            input=body,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, output[-12000:]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def path_mtime(path: Path) -> str | None:
    try:
        if path.is_dir():
            items = [item for item in path.rglob("*") if item.is_file()]
            stamp = max((item.stat().st_mtime for item in items), default=path.stat().st_mtime)
        else:
            stamp = path.stat().st_mtime
        return datetime.fromtimestamp(stamp, TZ).isoformat(timespec="seconds")
    except Exception:
        return None


def root_crontab() -> list[str]:
    ok, output = command([str(CRONTAB), "-l"], timeout=8)
    return output.splitlines() if ok else []


def write_root_crontab(lines: list[str]) -> tuple[bool, str]:
    content = "\n".join(lines).rstrip() + "\n"
    return command_input([str(CRONTAB), "-"], content, timeout=10)


def scheduled_tasks() -> list[dict]:
    lines = root_crontab()
    tasks = []
    for task_id, spec in TASK_SPECS.items():
        match = next((line for line in lines if spec["marker"] in line), "")
        paused = match.lstrip().startswith("# CONTROL-PAUSED ")
        tasks.append({
            "id": task_id,
            "label": spec["label"],
            "schedule": spec["schedule"],
            "present": bool(match),
            "enabled": bool(match and not paused),
            "last_run_at": path_mtime(spec["log"]),
        })
    return tasks


def set_task_enabled(task_id: str, enabled: bool) -> tuple[bool, str]:
    spec = TASK_SPECS.get(task_id)
    if not spec:
        return False, "未知计划任务"
    lines = root_crontab()
    found = False
    changed = False
    updated = []
    for line in lines:
        if spec["marker"] not in line:
            updated.append(line)
            continue
        found = True
        if enabled and line.lstrip().startswith("# CONTROL-PAUSED "):
            prefix_index = line.index("# CONTROL-PAUSED ")
            line = line[:prefix_index] + line[prefix_index + len("# CONTROL-PAUSED "):]
            changed = True
        elif not enabled and not line.lstrip().startswith("# CONTROL-PAUSED "):
            line = "# CONTROL-PAUSED " + line
            changed = True
        updated.append(line)
    if not found:
        return False, "计划任务未安装"
    if not changed:
        return True, "计划任务状态无需变更"
    ok, detail = write_root_crontab(updated)
    return ok, detail or ("已启用" if enabled else "已暂停")


def channel_file_count(name: str) -> int:
    try:
        return sum(1 for item in (WEIXIN_ROOT / name).glob("*.json") if item.is_file())
    except Exception:
        return 0


def notification_status() -> list[dict]:
    telegram_cfg = load_json(TELEGRAM_CONFIG)
    email_cfg = load_json(EMAIL_CONFIG)
    email_log = BOT_DIR / "email_notify.log"
    email_lines = []
    try:
        email_lines = [line.strip() for line in email_log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except Exception:
        pass
    sent_dir = WEIXIN_ROOT / "sent"
    return [
        {
            "id": "telegram",
            "label": "Telegram",
            "ready": bool(systemd_active("tg-node-bot.service") and (telegram_cfg.get("bot_token") or telegram_cfg.get("token"))),
            "detail": "Bot 在线，自动通知主通道" if systemd_active("tg-node-bot.service") else "Bot 服务未运行",
            "last_at": path_mtime(BOT_DIR / "bot.log"),
        },
        {
            "id": "wechat",
            "label": "微信",
            "ready": bool(systemd_active("weixin-bridge.service") and systemd_active("weixin-push-worker.service")),
            "detail": f"待发 {channel_file_count('queue')} · 成功 {channel_file_count('sent')} · 失败 {channel_file_count('failed')}",
            "last_at": path_mtime(sent_dir),
        },
        {
            "id": "email",
            "label": "邮件",
            "ready": bool((email_cfg.get("smtp") or {}).get("host") or (email_cfg.get("brevo_api") or {}).get("api_key") or (email_cfg.get("resend_api") or {}).get("api_key")),
            "detail": "Brevo 主通道 · Resend 备用" if email_cfg else "邮件配置未找到",
            "last_at": path_mtime(email_log),
            "last_result": email_lines[-1][:180] if email_lines else "暂无发送记录",
        },
    ]


def telegram_test(message: str) -> tuple[bool, str]:
    cfg = load_json(TELEGRAM_CONFIG)
    token = cfg.get("bot_token") or cfg.get("token") or cfg.get("TELEGRAM_BOT_TOKEN")
    raw_ids = cfg.get("chat_ids") or cfg.get("chat_id") or cfg.get("TELEGRAM_CHAT_ID") or []
    chat_ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
    if not token or not chat_ids:
        return False, "Telegram token 或 chat id 缺失"
    delivered = 0
    for chat_id in chat_ids:
        payload = json.dumps({
            "chat_id": str(chat_id),
            "text": message,
            "disable_web_page_preview": True,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
            delivered += 1
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return True, f"已发送到 {delivered} 个会话"


def test_notification(channel: str) -> tuple[bool, str]:
    message = f"控制台通知测试\n时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}\n渠道：{channel}"
    if channel == "telegram":
        return telegram_test(message)
    if channel == "wechat":
        if not WEIXIN_NOTIFY.exists():
            return False, "微信队列入口不存在"
        return command_input([str(WEIXIN_NOTIFY)], message, timeout=8)
    if channel == "email":
        if not EMAIL_NOTIFY.exists():
            return False, "邮件脚本不存在"
        return command_input(["/usr/bin/python3", str(EMAIL_NOTIFY), "--subject", "控制台邮件通道测试成功"], message, timeout=40)
    return False, "未知通知渠道"


def backup_status() -> dict:
    archives = []
    for path in sorted(BACKUP_DIR.glob("xushuo-control-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)[:14]:
        stat = path.stat()
        archives.append({
            "name": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, TZ).isoformat(timespec="seconds"),
        })
    return {"retention": 14, "archives": archives}


def verify_latest_backup() -> tuple[bool, str]:
    archives = sorted(BACKUP_DIR.glob("xushuo-control-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not archives:
        return False, "暂无备份可校验"
    return command(["/usr/bin/tar", "-tzf", str(archives[0])], timeout=90)


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
        "notifications": notification_status(),
        "tasks": scheduled_tasks(),
        "backups": backup_status(),
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

        if action == "run_task":
            spec = TASK_SPECS.get(target)
            if not spec:
                return 400, {"success": False, "message": "该任务不在执行白名单"}
            ok, detail = command(spec["run"], timeout=300)
            record_event(action, target, ok, detail or "任务执行完成")
            return (200 if ok else 500), {"success": ok, "message": f"{spec['label']} 已执行" if ok else f"{spec['label']} 执行失败", "detail": detail[-2000:]}

        if action in {"enable_task", "disable_task"}:
            enabled = action == "enable_task"
            ok, detail = set_task_enabled(target, enabled)
            label = (TASK_SPECS.get(target) or {}).get("label", target)
            record_event(action, target, ok, detail)
            verb = "启用" if enabled else "暂停"
            return (200 if ok else 500), {"success": ok, "message": f"{label} 已{verb}" if ok else f"{label}{verb}失败", "detail": detail[-1000:]}

        if action == "test_notification":
            if target not in {"telegram", "wechat", "email"}:
                return 400, {"success": False, "message": "该通知渠道不在白名单"}
            ok, detail = test_notification(target)
            record_event(action, target, ok, detail or "测试通知已提交")
            return (200 if ok else 500), {"success": ok, "message": "测试通知已发送" if ok else "测试通知发送失败", "detail": detail[-1000:]}

        if action == "create_backup":
            ok, detail = command(["/usr/bin/python3", str(BACKUP_SCRIPT)], timeout=300)
            record_event(action, "control-plane", ok, detail or "备份完成")
            return (200 if ok else 500), {"success": ok, "message": "控制中枢备份已创建" if ok else "备份创建失败", "detail": detail[-2000:]}

        if action == "verify_backup":
            ok, detail = verify_latest_backup()
            record_event(action, "latest", ok, "最新归档可正常读取" if ok else detail)
            return (200 if ok else 500), {"success": ok, "message": "最新备份校验通过" if ok else "备份校验失败", "detail": detail[-2000:]}

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
