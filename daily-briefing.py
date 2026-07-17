#!/usr/bin/env python3
"""Build the single daily morning report for Telegram and WeChat."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import clipboard_reminder
import market_alert
import service_watchdog


TZ = ZoneInfo("Asia/Shanghai")
STATUS_JSON = Path("/srv/vps-status/vps-status.json")
IP_REPORT = Path("/root/tg-node-bot/tg_report_combined_all.sh")
DASHBOARD_SCRIPT = Path("/opt/xushuo-control/daily-dashboard.py")


def dashboard_snapshot() -> dict:
    try:
        result = subprocess.run(
            ["/usr/bin/python3", str(DASHBOARD_SCRIPT), "--json"],
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def weather_section(snapshot: dict) -> list[str]:
    weather = snapshot.get("weather") or {}
    if not weather or weather.get("temperature") is None:
        return ["• 天气暂时不可用"]
    rain = weather.get("rain_probability")
    rain_text = f"｜降水 {float(rain):.0f}%" if rain is not None else ""
    low = weather.get("low")
    high = weather.get("high")
    range_text = f"{float(low):.0f}~{float(high):.0f}°C" if low is not None and high is not None else "温度范围暂缺"
    return [
        f"• {weather.get('city', '当前城市')}  {weather.get('condition', '--')}  {float(weather.get('temperature')):.0f}°C",
        f"• {range_text}{rain_text}",
    ]


def fund_section(snapshot: dict) -> list[str]:
    funds = snapshot.get("funds") or {}
    if not funds.get("total"):
        return ["• 基金数据暂时不可用"]
    daily = float(funds.get("daily_pnl") or 0)
    hold = float(funds.get("hold_pnl") or 0)
    return [
        f"• 已更新 {funds.get('updated', 0)}/{funds.get('total', 0)}｜持仓 {float(funds.get('total_amount') or 0):,.0f} 元",
        f"• 最新净值日盈亏 {daily:+,.0f} 元｜总盈亏 {hold:+,.0f} 元",
    ]


def vps_section() -> list[str]:
    try:
        data = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        servers = data.get("servers") or []
        offline = [str(item.get("name") or "未命名") for item in servers if not item.get("online")]
        online = len(servers) - len(offline)
        lines = [f"• 在线 {online}/{len(servers)}"]
        lines.append("• 异常：" + ("、".join(offline) if offline else "无"))
        return lines
    except Exception as exc:
        return [f"• 状态读取失败：{type(exc).__name__}"]


def reminder_section(now: datetime) -> list[str]:
    rows = clipboard_reminder.list_pending(limit=50)
    today = []
    for row in rows:
        try:
            due = datetime.fromisoformat(row["due_at"]).astimezone(TZ)
        except Exception:
            continue
        if due.date() == now.date():
            content = re.sub(r"\s+", " ", str(row.get("text") or ""))[:36]
            today.append(f"• {due.strftime('%H:%M')}  {content}")
    return today or ["• 今天暂无提醒"]


def service_section(now: datetime) -> list[str]:
    state = service_watchdog.load_state()
    healthy = int(state.get("healthy_count") or 0)
    total = int(state.get("total_count") or len(service_watchdog.SERVICES))
    cutoff = now - timedelta(hours=24)
    events = []
    for event in state.get("events", []):
        try:
            if datetime.fromisoformat(event["at"]).astimezone(TZ) >= cutoff:
                events.append(event)
        except Exception:
            continue
    lines = [f"• 健康 {healthy}/{total}｜24小时修复 {len(events)} 次"]
    for event in events[-3:]:
        icon = "已恢复" if event.get("result") == "recovered" else "失败"
        lines.append(f"• {event.get('label')}：{icon}")
    return lines


def run_ip_report() -> str:
    env = {"IP_SENTINEL_DRY_RUN": "1"}
    result = subprocess.run(
        ["/bin/bash", str(IP_REPORT)],
        text=True,
        capture_output=True,
        timeout=150,
        env={**__import__("os").environ, **env},
        check=False,
    )
    if result.returncode != 0:
        return result.stdout + "\n" + result.stderr
    return result.stdout


def ip_section() -> list[str]:
    try:
        report = run_ip_report()
    except Exception as exc:
        return [f"• 净化报告拉取失败：{type(exc).__name__}"]

    blocks = re.split(r"(?=^• )", report, flags=re.M)
    lines = []
    for block in blocks:
        header = re.search(r"^•\s+(.+?)\s+\(([^)]+)\)", block, re.M)
        if not header:
            continue
        name, region = header.group(1).strip(), header.group(2).strip()
        google = re.search(r"Google：.*?胜率\s+([\d.]+)%", block)
        trust = re.search(r"Trust：.*?成功率\s+([\d.]+)%", block)
        status = re.search(r"状态：(.+)", block)
        if google and trust:
            g, t = float(google.group(1)), float(trust.group(1))
            icon = "✅" if min(g, t) >= 90 else "⚠️"
            lines.append(f"• {icon} {name}({region})  G {g:.0f}%｜T {t:.0f}%")
        elif status:
            lines.append(f"• ⚠️ {name}({region})  {status.group(1).strip()[:46]}")
        else:
            lines.append(f"• ⚠️ {name}({region})  暂无有效报告")
    return lines or ["• 没有解析到净化节点"]


def build_briefing() -> str:
    now = datetime.now(TZ)
    snapshot = dashboard_snapshot()
    lines = ["☀️ xushuo 每日晨报", f"🕒 {now.strftime('%Y-%m-%d %H:%M')}"]
    lines.extend(["", "🌤 今日天气", *weather_section(snapshot)])
    lines.extend(["", "📡 VPS", *vps_section()])
    lines.extend(["", "🛠 核心服务", *service_section(now)])
    lines.extend(["", "⏰ 今日日程与待办", *reminder_section(now)])
    lines.extend(["", "💰 基金概览", *fund_section(snapshot)])
    lines.extend(["", "🛡 IP 净化", *ip_section()])
    return "\n".join(lines)


def main() -> None:
    import sys

    text = build_briefing()
    print(text)
    if "--dry-run" not in sys.argv:
        market_alert.tg_send(text)


if __name__ == "__main__":
    main()
