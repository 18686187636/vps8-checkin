"""签到状态管理：记录上次成功签到的北京时间日期，用于「当天已签到就跳过」。

状态文件：项目根目录下 .checkin_state/last_success.txt
内容：北京时间日期，格式 YYYY-MM-DD（单行）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".checkin_state"
STATE_FILE = STATE_DIR / "last_success.txt"

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_today() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def beijing_now_str() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def read_last_success() -> str | None:
    if not STATE_FILE.exists():
        return None
    try:
        text = STATE_FILE.read_text(encoding="utf-8").strip()
    except Exception as exc:
        print(f"[state] 读取状态文件失败: {exc}")
        return None
    return text or None


def already_checked_in_today() -> bool:
    last = read_last_success()
    today = beijing_today()
    if last == today:
        print(f"[state] 状态文件显示 {today}（北京时间）已签到，跳过本次运行")
        return True
    if last:
        print(f"[state] 上次成功签到: {last}；今天: {today}")
    else:
        print("[state] 无历史签到记录")
    return False


def mark_success() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    today = beijing_today()
    try:
        STATE_FILE.write_text(today + "\n", encoding="utf-8")
        print(f"[state] 已写入签到状态: {today}（{beijing_now_str()}）")
    except Exception as exc:
        print(f"[state] 写入状态文件失败: {exc}")
