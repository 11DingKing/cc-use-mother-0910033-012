"""时间工具：ISO-8601 归一化比较。"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_dt(value: str) -> datetime:
    """解析 ISO 时间；naive 时间按 UTC 处理，返回带时区的 datetime。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_before(a: str, b: str) -> bool:
    return parse_dt(a) < parse_dt(b)


def is_at_or_before(a: str, b: str) -> bool:
    return parse_dt(a) <= parse_dt(b)
