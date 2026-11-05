"""SQLite durum deposu: ilham gönderileri + taslaklar."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional

from .config import DB_PATH, TZ

# Taslak durumları
IDEA_PENDING = "idea_pending"        # fikir Telegram'da onay bekliyor
DESIGNING = "designing"              # onaylandı, tasarım üretiliyor
DESIGN_PENDING = "design_pending"    # tasarım + açıklama paylaşım onayı bekliyor
PUBLISHING = "publishing"
SCHEDULED = "scheduled"              # Zernio'da zamanlandı
PUBLISHED = "published"
REJECTED = "rejected"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspiration (
    id TEXT PRIMARY KEY,
    owner TEXT,
    url TEXT,
    type TEXT,
    caption TEXT,
    likes INTEGER,
    comments_count INTEGER,
    views INTEGER,
    comments_json TEXT,
    image_url TEXT,
    posted_at TEXT,
    scraped_at TEXT,
    used_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    idea_json TEXT NOT NULL,
    content_json TEXT,
    images_json TEXT,
    caption TEXT,
    scheduled_for TEXT,
    zernio_post_id TEXT,
    permalink TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


# ---------- kv ----------
def kv_get(k: str) -> Optional[str]:
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None


def kv_set(k: str, v: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# ---------- inspiration ----------
def upsert_inspiration(items: List[Dict[str, Any]]) -> int:
    with conn() as c:
        for it in items:
            c.execute(
                """INSERT INTO inspiration(id,owner,url,type,caption,likes,comments_count,views,
                   comments_json,image_url,posted_at,scraped_at)
                   VALUES(:id,:owner,:url,:type,:caption,:likes,:comments_count,:views,
                   :comments_json,:image_url,:posted_at,:scraped_at)
                   ON CONFLICT(id) DO UPDATE SET likes=excluded.likes,
                   comments_count=excluded.comments_count, views=excluded.views,
                   comments_json=excluded.comments_json, image_url=excluded.image_url,
                   scraped_at=excluded.scraped_at""",
                it,
            )
    return len(items)


def top_inspiration(limit: int) -> List[sqlite3.Row]:
    """Etkileşime göre en iyi, az kullanılmış, son 60 günün gönderileri."""
    with conn() as c:
        return c.execute(
            """SELECT * FROM inspiration
               WHERE posted_at >= date('now','-60 day') OR posted_at IS NULL
               ORDER BY used_count ASC,
                        (COALESCE(likes,0) + 4*COALESCE(comments_count,0)) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def get_inspiration(ids: List[str]) -> List[sqlite3.Row]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    with conn() as c:
        return c.execute(f"SELECT * FROM inspiration WHERE id IN ({q})", ids).fetchall()


def mark_inspiration_used(ids: List[str]) -> None:
    if not ids:
        return
    with conn() as c:
        c.executemany("UPDATE inspiration SET used_count=used_count+1 WHERE id=?", [(i,) for i in ids])


# ---------- drafts ----------
def create_draft(idea: Dict[str, Any]) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO drafts(status,idea_json,created_at,updated_at) VALUES(?,?,?,?)",
            (IDEA_PENDING, json.dumps(idea, ensure_ascii=False), now_iso(), now_iso()),
        )
        return int(cur.lastrowid)


def get_draft(draft_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def update_draft(draft_id: int, **fields: Any) -> None:
    for k in ("idea_json", "content_json", "images_json"):
        if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE drafts SET {cols} WHERE id=?", (*fields.values(), draft_id))


def drafts_by_status(*statuses: str) -> List[sqlite3.Row]:
    q = ",".join("?" * len(statuses))
    with conn() as c:
        return c.execute(
            f"SELECT * FROM drafts WHERE status IN ({q}) ORDER BY id", statuses
        ).fetchall()


def recent_titles(limit: int = 40) -> List[str]:
    with conn() as c:
        rows = c.execute(
            "SELECT idea_json FROM drafts WHERE status != ? ORDER BY id DESC LIMIT ?",
            (REJECTED, limit),
        ).fetchall()
    return [json.loads(r["idea_json"]).get("title", "") for r in rows]


def portfolio_count(days: int = 7) -> int:
    with conn() as c:
        rows = c.execute(
            "SELECT idea_json FROM drafts WHERE status != ? AND created_at >= ?",
            (REJECTED, (datetime.now(TZ) - timedelta(days=days)).isoformat()),
        ).fetchall()
    return sum(1 for r in rows if json.loads(r["idea_json"]).get("format") == "portfolio")


def recent_portfolios(limit: int = 8) -> List[str]:
    with conn() as c:
        rows = c.execute("SELECT idea_json FROM drafts ORDER BY id DESC LIMIT 60").fetchall()
    out = []
    for r in rows:
        i = json.loads(r["idea_json"])
        if i.get("format") == "portfolio":
            out.append(f"{i.get('title')} — {i.get('visual_concept', '')[:80]}")
    return out[:limit]


def taken_slots() -> List[str]:
    with conn() as c:
        rows = c.execute(
            "SELECT scheduled_for FROM drafts WHERE status IN (?,?,?) AND scheduled_for IS NOT NULL",
            (SCHEDULED, PUBLISHING, PUBLISHED),
        ).fetchall()
    return [r["scheduled_for"] for r in rows]


# ---------- telegram message ↔ draft ----------
def link_message(message_id: int, draft_id: int) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO messages(message_id,draft_id) VALUES(?,?)", (message_id, draft_id)
        )


def draft_for_message(message_id: int) -> Optional[int]:
    with conn() as c:
        r = c.execute("SELECT draft_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return int(r["draft_id"]) if r else None
