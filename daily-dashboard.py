#!/usr/bin/env python3
"""Build cached data for the private daily dashboard and morning briefing."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
BOT_DIR = Path("/root/tg-node-bot")
OUTPUT = Path("/var/lib/xushuo-control/daily-dashboard.json")
STATUS_JSON = Path("/srv/vps-status/vps-status.json")
MARKET_CACHE = BOT_DIR / "market_alert_cache.json"
AZURE_CACHE = BOT_DIR / "azure_monitor_traffic_cache.json"
WATCHDOG_STATE = BOT_DIR / "service_watchdog_state.json"
CITY = {
    "name": "哈尔滨",
    "latitude": 45.75,
    "longitude": 126.63,
}

WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "强雷雨伴冰雹",
}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def weather_data() -> dict:
    params = urllib.parse.urlencode({
        "latitude": CITY["latitude"],
        "longitude": CITY["longitude"],
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 2,
        "timezone": "Asia/Shanghai",
    })
    request = urllib.request.Request(
        f"https://api.open-meteo.com/v1/forecast?{params}",
        headers={"User-Agent": "xushuo-dashboard/1.0"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    code = int(current.get("weather_code") or 0)
    return {
        "city": CITY["name"],
        "condition": WEATHER_CODES.get(code, f"天气代码 {code}"),
        "temperature": current.get("temperature_2m"),
        "feels_like": current.get("apparent_temperature"),
        "wind": current.get("wind_speed_10m"),
        "high": (daily.get("temperature_2m_max") or [None])[0],
        "low": (daily.get("temperature_2m_min") or [None])[0],
        "rain_probability": (daily.get("precipitation_probability_max") or [None])[0],
        "observed_at": current.get("time"),
        "error": "",
    }


def reminder_data(now: datetime) -> dict:
    sys.path.insert(0, str(BOT_DIR))
    try:
        import clipboard_reminder

        rows = clipboard_reminder.list_pending(limit=20)
    except Exception as exc:
        return {"today": [], "upcoming": [], "error": f"{type(exc).__name__}: {exc}"}

    today = []
    upcoming = []
    for row in rows:
        try:
            due = datetime.fromisoformat(str(row.get("due_at"))).astimezone(TZ)
        except Exception:
            continue
        item = {
            "id": int(row.get("id") or 0),
            "due_at": due.isoformat(timespec="minutes"),
            "time": due.strftime("%H:%M"),
            "date": due.strftime("%m-%d"),
            "text": re.sub(r"\s+", " ", str(row.get("text") or "")).strip()[:80],
        }
        (today if due.date() == now.date() else upcoming).append(item)
    return {"today": today[:8], "upcoming": upcoming[:8], "error": ""}


def fund_data(now: datetime) -> dict:
    cache = load_json(MARKET_CACHE, {})
    rows = []
    for item in cache.get("fund_items") or []:
        if item.get("fund_type") == "watch":
            continue
        nav = float(item.get("nav") or 0)
        base_nav = float(item.get("base_nav") or 0)
        shares = float(item.get("shares") or 0)
        daily_pnl = shares * (nav - base_nav) if nav and base_nav and shares else 0.0
        rows.append({
            "code": str(item.get("code") or ""),
            "name": str(item.get("name") or ""),
            "type": str(item.get("fund_type") or "normal"),
            "nav": nav,
            "nav_pct": float(item.get("nav_pct") or 0),
            "nav_date": str(item.get("nav_date") or ""),
            "amount": float(item.get("amount") or 0),
            "daily_pnl": round(daily_pnl, 2),
            "hold_pnl": round(float(item.get("hold_pnl") or 0), 2),
            "updated_today": str(item.get("nav_date") or "") == now.strftime("%Y-%m-%d"),
        })
    return {
        "items": rows,
        "total_amount": round(sum(item["amount"] for item in rows), 2),
        "daily_pnl": round(sum(item["daily_pnl"] for item in rows), 2),
        "hold_pnl": round(sum(item["hold_pnl"] for item in rows), 2),
        "updated": sum(1 for item in rows if item["updated_today"]),
        "total": len(rows),
        "source_at": cache.get("saved_at"),
        "errors": cache.get("fund_errors") or [],
    }


def fleet_data() -> dict:
    data = load_json(STATUS_JSON, {})
    servers = data.get("servers") or []
    offline = [str(item.get("name") or "未命名") for item in servers if not item.get("online")]
    return {
        "online": len(servers) - len(offline),
        "total": len(servers),
        "offline": offline,
        "source_at": data.get("updated_at"),
    }


def service_data() -> dict:
    state = load_json(WATCHDOG_STATE, {})
    return {
        "healthy": int(state.get("healthy_count") or 0),
        "total": int(state.get("total_count") or 0),
        "checked_at": state.get("last_scan_at"),
        "recent_events": list((state.get("events") or [])[-5:]),
    }


def cloud_data() -> dict:
    data = load_json(AZURE_CACHE, {})
    accounts = data.get("accounts") or []
    return {
        "azure_accounts": len(accounts),
        "used_gb": round(sum(float(item.get("total_out_gb") or 0) for item in accounts), 2),
        "quota_gb": round(sum(float(item.get("quota_gb") or 0) for item in accounts), 2),
        "source_at": data.get("cached_at"),
    }


def build() -> dict:
    now = datetime.now(TZ)
    try:
        weather = weather_data()
    except Exception as exc:
        previous = load_json(OUTPUT, {}).get("weather") or {}
        weather = {**previous, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "success": True,
        "updated_at": now.isoformat(timespec="seconds"),
        "weather": weather,
        "reminders": reminder_data(now),
        "funds": fund_data(now),
        "fleet": fleet_data(),
        "services": service_data(),
        "cloud": cloud_data(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = build()
    atomic_json(OUTPUT, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
