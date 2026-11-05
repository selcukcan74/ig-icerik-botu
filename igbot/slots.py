"""Günlük 1-2 gönderilik yayın slotlarını hesaplar."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import List, Optional

from . import db
from .config import CFG, TZ


def _slot_times() -> List[time]:
    p = CFG["posting"]
    n = int(p.get("posts_per_day", 1))
    out = []
    for s in p["slots"][:n]:
        h, m = s.split(":")
        out.append(time(int(h), int(m)))
    return out


def _taken_minutes() -> set:
    taken = set()
    for s in db.taken_slots():
        try:
            dt = datetime.fromisoformat(s)
            taken.add(dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            pass
    return taken


def posts_on_day(day) -> int:
    return sum(
        1 for s in db.taken_slots() if datetime.fromisoformat(s).astimezone(TZ).date() == day
    )


def upcoming_free_slots(count: int = 6, days: int = 14) -> List[datetime]:
    """Dolu olmayan, günlük limiti aşmayan sonraki slotlar."""
    now = datetime.now(TZ) + timedelta(minutes=10)
    taken = _taken_minutes()
    limit = int(CFG["posting"].get("posts_per_day", 1))
    out: List[datetime] = []
    for d in range(days):
        day = (now + timedelta(days=d)).date()
        used = posts_on_day(day)
        for t in _slot_times():
            dt = datetime.combine(day, t, tzinfo=TZ)
            if dt <= now or dt.strftime("%Y-%m-%d %H:%M") in taken:
                continue
            if used >= limit:
                break
            out.append(dt)
            used += 1
            if len(out) >= count:
                return out
    return out


def next_free_slot() -> Optional[datetime]:
    s = upcoming_free_slots(1)
    return s[0] if s else None


def parse_user_time(text: str) -> Optional[datetime]:
    """'18:30', 'yarın 18:30', '07.11 18:30', '07.11.2026 18:30' biçimlerini kabul eder."""
    text = text.strip().lower()
    now = datetime.now(TZ)
    day = now.date()
    if text.startswith("yarın") or text.startswith("yarin"):
        day = day + timedelta(days=1)
        text = text.split(maxsplit=1)[1] if " " in text else ""
    parts = text.split()
    try:
        if len(parts) == 2:
            dpart, tpart = parts
            bits = dpart.replace("/", ".").split(".")
            dd, mm = int(bits[0]), int(bits[1])
            yy = int(bits[2]) if len(bits) > 2 and bits[2] else now.year
            if yy < 100:
                yy += 2000
            day = datetime(yy, mm, dd).date()
        elif len(parts) == 1:
            tpart = parts[0]
        else:
            return None
        h, m = tpart.replace(".", ":").split(":")
        dt = datetime.combine(day, time(int(h), int(m)), tzinfo=TZ)
    except (ValueError, IndexError):
        return None
    return dt if dt > now + timedelta(minutes=5) else None


def queue_days() -> float:
    """Onay bekleyen + planlanmış içerik kaç günlük yayın eder."""
    n = len(
        db.drafts_by_status(db.IDEA_PENDING, db.DESIGNING, db.DESIGN_PENDING, db.SCHEDULED)
    )
    return n / max(1, int(CFG["posting"].get("posts_per_day", 1)))


TR_DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def fmt(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    return f"{TR_DAYS[dt.weekday()]} {dt.strftime('%d.%m %H:%M')}"
