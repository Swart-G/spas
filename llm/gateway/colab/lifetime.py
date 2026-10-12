"""VM lifetime estimates are distinct from short-lived proxy-token expiry."""

import time


def vm_time(record, *, now=None):
    now = time.time() if now is None else now
    start = record.get("runtime_started_at")
    hours = record.get("settings", {}).get("vm_lifetime_hours", 12)
    expired = record.get("stage") in {"EXPIRED", "DELETED"}
    elapsed = max(0, now - start) if isinstance(start, (int, float)) else None
    return {
        "started_at": start,
        "elapsed_seconds": round(elapsed) if elapsed is not None else None,
        "remaining_seconds": 0
        if expired
        else max(0, round(hours * 3600 - elapsed))
        if elapsed is not None
        else None,
        "lifetime_hours": hours,
        "source": "estimate",
        "expired": expired,
    }


def time_label(record):
    data = vm_time(record)
    if data["expired"]:
        return "VM завершена"
    remaining = data["remaining_seconds"]
    if remaining is None:
        return "Неизвестно"
    if remaining == 0:
        return "Оценочный лимит достигнут"
    return f"≈ {remaining // 3600}ч {(remaining % 3600) // 60}м"
