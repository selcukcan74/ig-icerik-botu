"""İlham hesaplarının gönderilerini + yorumlarını Apify ile çeker."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

import httpx

from . import db
from .config import CFG, TZ, env

log = logging.getLogger(__name__)

ACTOR = "apify~instagram-scraper"
RUN_SYNC = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"


def _normalize(raw: Dict[str, Any], max_comments: int) -> Dict[str, Any]:
    comments = []
    for c in (raw.get("latestComments") or [])[:max_comments]:
        text = (c.get("text") or "").strip()
        if text:
            comments.append({"text": text[:300], "likes": c.get("likesCount") or 0})
    return {
        "id": str(raw.get("id") or raw.get("shortCode")),
        "owner": raw.get("ownerUsername"),
        "url": raw.get("url"),
        "type": raw.get("type"),  # Image | Sidecar | Video
        "caption": (raw.get("caption") or "")[:2200],
        "likes": raw.get("likesCount") or 0,
        "comments_count": raw.get("commentsCount") or 0,
        "views": raw.get("videoViewCount") or raw.get("videoPlayCount") or 0,
        "comments_json": json.dumps(comments, ensure_ascii=False),
        "image_url": raw.get("displayUrl"),
        "posted_at": raw.get("timestamp"),
        "scraped_at": db.now_iso(),
    }


def scrape(urls: List[str] = None, limit: int = None) -> int:
    """Hesap ya da tekil gönderi URL'lerini tarar, DB'ye yazar. Kaydedilen gönderi sayısını döner."""
    ins = CFG["inspiration"]
    if urls is None:
        urls = [f"https://www.instagram.com/{u.strip('@')}/" for u in ins["accounts"] if u]
    if not urls:
        return 0
    payload = {
        "directUrls": urls,
        "resultsType": "posts",
        "resultsLimit": limit or ins.get("posts_per_account", 12),
        "addParentData": False,
    }
    log.info("Apify taraması başlıyor: %s", urls)
    r = httpx.post(
        RUN_SYNC,
        json=payload,
        headers={"Authorization": f"Bearer {env('APIFY_TOKEN')}"},
        timeout=httpx.Timeout(320.0),
    )
    r.raise_for_status()
    items = [x for x in r.json() if isinstance(x, dict) and not x.get("error")]
    norm = [_normalize(x, ins.get("max_comments_per_post", 8)) for x in items]
    norm = [n for n in norm if n["id"] and n["id"] != "None"]
    db.upsert_inspiration(norm)
    if urls and all("/p/" not in u and "/reel/" not in u for u in urls):
        db.kv_set("last_scrape", db.now_iso())
    log.info("Apify: %d gönderi kaydedildi", len(norm))
    return len(norm)


def scrape_if_due() -> int:
    last = db.kv_get("last_scrape")
    every = CFG["inspiration"].get("scrape_every_days", 3)
    if last and datetime.fromisoformat(last) > datetime.now(TZ) - timedelta(days=every):
        return 0
    return scrape()
