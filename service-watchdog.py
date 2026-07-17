#!/usr/bin/env python3
"""Health-check and repair the Azure SG control-plane services."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


BASE = Path("/root/tg-node-bot")
STATE_FILE = BASE / "service_watchdog_state.json"
CONFIG_FILE = BASE / "config.json"
WEIXIN_NOTIFY = Path("/usr/local/bin/weixin-notify")
EMAIL_NOTIFY = BASE / "email_notify.py"
TZ = ZoneInfo("Asia/Shanghai")
CHECK_INTERVAL = 30
FAILURES_BEFORE_REPAIR = 2
REPAIR_COOLDOWN_SECONDS = 10 * 60
MAX_REPAIRS_PER_HOUR = 3

SERVICES = [
    {"unit": "tg-node-bot.service", "label": "Telegram Bot"},
    {"unit": "weixin-bridge.service", "label": "微信 Bot"},
    {"unit": "weixin-push-worker.service", "label": "微信推送"},
    {"unit": "clipboard-reminder.service", "label": "收藏提醒"},
    {"unit": "ai-agent.service", "label": "AI 服务", "http": "http://127.0.0.1:8765/v1/health"},
    {"unit": "jp-auto-switcher.service", "label": "Japan Auto"},
    {
        "unit": "xushuo-control-api.service",
        "label": "统一控制 API",
        "http": "http://127.0.0.1:8790/control-api/status",
    },
    {
        "unit": "xushuo-tools-api.service",
        "label": "投递与 Webhook",
        "http": "http://127.0.0.1:8791/health",
    },
    {
        "unit": "caddy.service",
        "label": "Caddy",
        "http": "http://127.0.0.1/vps-status.json",
        "host": "status.xushuo.uk",
    },
    {"unit": "haproxy.service", "label": "HAProxy", "tcp": ("127.0.0.1", 443)},
    {"unit": "openlist.service", "label": "OpenList", "http": "http://127.0.0.1:5244/"},
    {"unit": "cliproxyapi.service", "label": "CPA", "tcp": ("127.0.0.1", 8317)},
    {"unit": "cloudflared.service", "label": "Cloudflare Tunnel"},
    {"unit": "cloudflared-pan.service", "label": "网盘 Tunnel"},
    {"unit": "marzban-node.service", "label": "Marzban Node", "tcp": ("127.0.0.1", 8010)},
    {"unit": "docker.service", "label": "Docker"},
]


def now_local() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state.setdefault("services", {})
            state.setdefault("events", [])
            return state
    except Exception:
        pass
    return {"version": 1, "services": {}, "events": []}


def save_state(state: dict) -> None:
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(STATE_FILE)


def command_ok(args: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
        output = (result.stderr or result.stdout or "").strip()
        return result.returncode == 0, output[:180]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:180]


def http_ok(url: str, host: str | None = None) -> tuple[bool, str]:
    headers = {"User-Agent": "xushuo-service-watchdog/1.0"}
    if host:
        headers["Host"] = host
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(256)
            return response.status < 500, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return exc.code < 500, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:180]


def tcp_ok(target: tuple[str, int]) -> tuple[bool, str]:
    try:
        with socket.create_connection(target, timeout=4):
            return True, f"TCP {target[1]}"
    except Exception as exc:
        return False, f"TCP {target[1]}: {type(exc).__name__}"[:180]


def check_service(spec: dict) -> tuple[bool, str]:
    active, detail = command_ok(["/usr/bin/systemctl", "is-active", "--quiet", spec["unit"]])
    if not active:
        return False, "systemd inactive" + (f" ({detail})" if detail else "")
    if spec.get("http"):
        return http_ok(spec["http"], spec.get("host"))
    if spec.get("tcp"):
        return tcp_ok(spec["tcp"])
    return True, "systemd active"


def restart_service(unit: str) -> tuple[bool, str]:
    return command_ok(["/usr/bin/systemctl", "restart", unit], timeout=30)


def _chat_config() -> tuple[str | None, list[str]]:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None, []
    token = cfg.get("bot_token") or cfg.get("token") or cfg.get("TELEGRAM_BOT_TOKEN")
    raw = cfg.get("chat_ids") or cfg.get("chat_id") or cfg.get("TELEGRAM_CHAT_ID") or []
    if not isinstance(raw, list):
        raw = [raw]
    return str(token) if token else None, [str(item) for item in raw if str(item).strip()]


def notify(text: str, urgent: bool = False) -> None:
    if WEIXIN_NOTIFY.exists():
        try:
            subprocess.run([str(WEIXIN_NOTIFY)], input=text, text=True, timeout=5, check=False)
        except Exception:
            pass
    token, chat_ids = _chat_config()
    if token:
        for chat_id in chat_ids:
            payload = json.dumps({
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }, ensure_ascii=False).encode("utf-8")
            try:
                request = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=12) as response:
                    response.read()
            except Exception:
                pass
    if urgent and EMAIL_NOTIFY.exists():
        try:
            subject = text.splitlines()[0][:80]
            subprocess.run(
                ["/usr/bin/python3", str(EMAIL_NOTIFY), "--subject", subject],
                input=text,
                text=True,
                timeout=35,
                check=False,
            )
        except Exception:
            pass


def _recent_repairs(entry: dict, now: datetime) -> list[str]:
    cutoff = now - timedelta(hours=1)
    result = []
    for value in entry.get("repair_attempts", []):
        try:
            if datetime.fromisoformat(value).astimezone(TZ) >= cutoff:
                result.append(value)
        except Exception:
            continue
    return result


def run_once(allow_repair: bool = True) -> dict:
    state = load_state()
    now = now_local()
    notifications = []

    for spec in SERVICES:
        unit = spec["unit"]
        entry = state["services"].setdefault(unit, {})
        healthy, reason = check_service(spec)
        entry.update({"label": spec["label"], "checked_at": now_iso(), "reason": reason})

        if healthy:
            entry["healthy"] = True
            entry["failures"] = 0
            entry["status"] = "healthy"
            continue

        entry["healthy"] = False
        entry["failures"] = int(entry.get("failures") or 0) + 1
        entry["status"] = "degraded"
        if not allow_repair or entry["failures"] < FAILURES_BEFORE_REPAIR:
            continue

        attempts = _recent_repairs(entry, now)
        entry["repair_attempts"] = attempts
        last_attempt = None
        if attempts:
            try:
                last_attempt = datetime.fromisoformat(attempts[-1]).astimezone(TZ)
            except Exception:
                last_attempt = None
        if len(attempts) >= MAX_REPAIRS_PER_HOUR or (
            last_attempt and (now - last_attempt).total_seconds() < REPAIR_COOLDOWN_SECONDS
        ):
            entry["status"] = "cooldown"
            continue

        attempt_at = now_iso()
        entry.setdefault("repair_attempts", []).append(attempt_at)
        restarted, restart_detail = restart_service(unit)
        time.sleep(5)
        recovered, verify_reason = check_service(spec)
        event = {
            "at": now_iso(),
            "unit": unit,
            "label": spec["label"],
            "result": "recovered" if restarted and recovered else "failed",
            "reason": reason,
            "verify": verify_reason if restarted else restart_detail,
        }
        state["events"].append(event)
        entry.update({
            "healthy": bool(recovered),
            "failures": 0 if recovered else entry["failures"],
            "status": "recovered" if recovered else "failed",
            "last_repair_at": event["at"],
            "reason": verify_reason if recovered else event["verify"],
        })
        if recovered:
            notifications.append((
                f"🛠 服务自动修复成功｜{spec['label']}\n"
                f"原因：{reason}\n恢复：{verify_reason}\n时间：{now.strftime('%m-%d %H:%M:%S')}",
                False,
            ))
        else:
            notifications.append((
                f"🚨 服务自动修复失败｜{spec['label']}\n"
                f"原因：{reason}\n复查：{event['verify']}\n时间：{now.strftime('%m-%d %H:%M:%S')}",
                True,
            ))

    state["events"] = state["events"][-200:]
    state["last_scan_at"] = now_iso()
    state["healthy_count"] = sum(1 for value in state["services"].values() if value.get("healthy"))
    state["total_count"] = len(SERVICES)
    save_state(state)
    for message, urgent in notifications:
        notify(message, urgent=urgent)
    return state


def status_text(state: dict | None = None) -> str:
    state = state or load_state()
    healthy = int(state.get("healthy_count") or 0)
    total = int(state.get("total_count") or len(SERVICES))
    lines = ["🛠 服务自动修复", f"健康 {healthy}/{total}", f"检查：{state.get('last_scan_at') or '尚未运行'}"]
    bad = [value for value in state.get("services", {}).values() if not value.get("healthy")]
    if bad:
        lines.extend(["", "异常"])
        lines.extend(f"• {item.get('label')}：{item.get('status')}｜{item.get('reason')}" for item in bad)
    recent = state.get("events", [])[-5:]
    if recent:
        lines.extend(["", "最近修复"])
        for item in reversed(recent):
            icon = "✅" if item.get("result") == "recovered" else "❌"
            lines.append(f"• {icon} {item.get('label')}｜{item.get('at', '')[5:16].replace('T', ' ')}")
    return "\n".join(lines)


def main() -> None:
    import sys

    if "--status" in sys.argv:
        print(status_text())
        return
    if "--check" in sys.argv:
        print(status_text(run_once(allow_repair=False)))
        return
    if "--once" in sys.argv:
        print(status_text(run_once(allow_repair=True)))
        return
    while True:
        try:
            run_once(allow_repair=True)
        except Exception as exc:
            print(f"watchdog scan failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
